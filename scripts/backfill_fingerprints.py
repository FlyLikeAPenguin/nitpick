#!/usr/bin/env python3
"""Recompute error_group fingerprints with the current normalizer and merge collisions.

Groups are fingerprinted from the raw sample log, so changing normalize_message()
leaves historical rows keyed under fingerprints the live pipeline will never
produce again. Every one of those rows then looks like a brand-new error on the
next run and earns a fresh paid investigation.

This backfills them: recompute each group's fingerprint from its stored
sample_log_json, merge groups that now collapse together, and repoint their
investigations. Merged groups inherit the most advanced status of their members,
so a backlog entry absorbed into an already-documented group stops being a
re-investigation candidate.

Idempotent — a second run is a no-op. Dry run by default.

    python -m scripts.backfill_fingerprints            # report only
    python -m scripts.backfill_fingerprints --apply    # migrate (backs up first)
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.fingerprint import compute_fingerprint, extract_error_fields, normalize_message

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "error_cache.db"

# Which status wins when groups merge. A group absorbed into a documented one
# must not fall back to 'new' — that would re-queue it for investigation.
STATUS_RANK = {"fixed": 4, "documented": 3, "investigating": 2, "new": 1, "failed": 0}


def recompute(conn: sqlite3.Connection) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Map each existing fingerprint to the one the current normalizer produces."""
    mapping: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    for row in conn.execute("SELECT fingerprint, service, sample_log_json FROM error_groups"):
        try:
            sample = json.loads(row["sample_log_json"] or "")
        except (json.JSONDecodeError, TypeError):
            skipped.append((row["fingerprint"], "sample_log_json unusable"))
            continue
        error_type, message = extract_error_fields(sample)
        if not message:
            skipped.append((row["fingerprint"], "no message in sample log"))
            continue
        mapping[row["fingerprint"]] = compute_fingerprint(row["service"], error_type, message)
    return mapping, skipped


def pick_canonical(conn: sqlite3.Connection, fingerprints: list[str]) -> sqlite3.Row:
    """The row whose metadata the merged group keeps.

    Prefer a group that already reached a tracker issue: its sample log and
    dd_query_link are the ones referenced from the filed issue.
    """
    rows = [
        conn.execute("SELECT * FROM error_groups WHERE fingerprint = ?", (fp,)).fetchone()
        for fp in fingerprints
    ]
    filed = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT fingerprint FROM investigations"
            " WHERE linear_issue_url IS NOT NULL AND linear_issue_url <> ''"
        )
    }
    return max(
        rows,
        key=lambda r: (
            r["fingerprint"] in filed,
            STATUS_RANK.get(r["status"], 0),
            -_epoch(r["first_seen"]),
        ),
    )


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).timestamp()


def merge_cluster(conn: sqlite3.Connection, new_fp: str, olds: list[str], apply: bool) -> None:
    canonical = pick_canonical(conn, olds)
    rows = [
        conn.execute("SELECT * FROM error_groups WHERE fingerprint = ?", (fp,)).fetchone()
        for fp in olds
    ]
    sample = json.loads(canonical["sample_log_json"])
    _, message = extract_error_fields(sample)

    merged = {
        "fingerprint": new_fp,
        "service": canonical["service"],
        "error_type": canonical["error_type"],
        "message_template": normalize_message(message),
        "first_seen": min(r["first_seen"] for r in rows),
        "last_seen": max(r["last_seen"] for r in rows),
        "occurrence_count": sum(r["occurrence_count"] or 0 for r in rows),
        "status": max((r["status"] for r in rows), key=lambda s: STATUS_RANK.get(s, 0)),
        "region": canonical["region"],
        "sample_log_json": canonical["sample_log_json"],
        "dd_query_link": canonical["dd_query_link"],
    }
    if not apply:
        return

    cols = ", ".join(merged)
    conn.execute(
        f"INSERT INTO error_groups ({cols}) VALUES ({', '.join('?' * len(merged))})"
        " ON CONFLICT(fingerprint) DO UPDATE SET"
        " service=excluded.service, error_type=excluded.error_type,"
        " message_template=excluded.message_template, first_seen=excluded.first_seen,"
        " last_seen=excluded.last_seen, occurrence_count=excluded.occurrence_count,"
        " status=excluded.status, region=excluded.region,"
        " sample_log_json=excluded.sample_log_json, dd_query_link=excluded.dd_query_link",
        tuple(merged.values()),
    )
    absorbed = [fp for fp in olds if fp != new_fp]
    if absorbed:
        marks = ", ".join("?" * len(absorbed))
        conn.execute(
            f"UPDATE investigations SET fingerprint = ? WHERE fingerprint IN ({marks})",
            (new_fp, *absorbed),
        )
        conn.execute(f"DELETE FROM error_groups WHERE fingerprint IN ({marks})", absorbed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    mapping, skipped = recompute(conn)
    clusters = collections.defaultdict(list)
    for old, new in mapping.items():
        clusters[new].append(old)

    changed = {new: olds for new, olds in clusters.items() if len(olds) > 1 or olds[0] != new}
    absorbed = sum(len(o) for o in changed.values()) - len(changed)
    requeued = sum(
        1
        for olds in changed.values()
        for fp in olds
        if conn.execute("SELECT status FROM error_groups WHERE fingerprint = ?", (fp,)).fetchone()[0]
        in ("new", "failed")
    )

    print(f"groups                 {len(mapping)}")
    print(f"unusable (left as-is)  {len(skipped)}")
    print(f"clusters to rewrite    {len(changed)}")
    print(f"groups absorbed        {absorbed}")
    print(f"after migration        {len(clusters)} groups")
    print(f"backlog rows involved  {requeued} (status new/failed)")
    for fp, _ in skipped[:10]:
        print(f"  skipped {fp}")

    if not args.apply:
        print("\ndry run — rerun with --apply to migrate")
        return 0

    backup = args.db.with_name(f"{args.db.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(args.db, backup)
    print(f"\nbackup: {backup}")

    with conn:
        for new_fp, olds in changed.items():
            merge_cluster(conn, new_fp, olds, apply=True)

    remaining = conn.execute("SELECT COUNT(*) FROM error_groups").fetchone()[0]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM investigations i"
        " LEFT JOIN error_groups e ON i.fingerprint = e.fingerprint"
        " WHERE e.fingerprint IS NULL"
    ).fetchone()[0]
    print(f"done — {remaining} groups, {orphans} orphaned investigations")
    return 1 if orphans else 0


if __name__ == "__main__":
    raise SystemExit(main())
