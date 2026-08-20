from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .models import ErrorGroup, Investigation

SCHEMA = """
CREATE TABLE IF NOT EXISTS error_groups (
    fingerprint     TEXT PRIMARY KEY,
    service         TEXT NOT NULL,
    error_type      TEXT,
    message_template TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'new',
    region          TEXT,
    sample_log_json TEXT,
    dd_query_link   TEXT
);

CREATE TABLE IF NOT EXISTS investigations (
    id                  TEXT PRIMARY KEY,
    fingerprint         TEXT NOT NULL REFERENCES error_groups(fingerprint),
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    root_cause          TEXT,
    is_actionable       INTEGER DEFAULT 0,
    is_fixable          INTEGER DEFAULT 0,
    is_duplicate        INTEGER DEFAULT 0,
    duplicate_of_url    TEXT DEFAULT '',
    estimated_fix_lines INTEGER,
    severity            TEXT DEFAULT 'medium',
    affected_files      TEXT,
    fix_description     TEXT,
    recommendation      TEXT,
    notion_page_url     TEXT,
    linear_issue_url    TEXT,
    linear_issue_id     TEXT,
    pr_url              TEXT,
    branch_name         TEXT,
    fix_error           TEXT DEFAULT '',
    claude_cost_usd     REAL DEFAULT 0.0,
    claude_input_tokens INTEGER DEFAULT 0,
    claude_output_tokens INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS run_log (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    errors_fetched  INTEGER DEFAULT 0,
    new_errors      INTEGER DEFAULT 0,
    investigated    INTEGER DEFAULT 0,
    issues_created  INTEGER DEFAULT 0,
    prs_created     INTEGER DEFAULT 0,
    total_cost_usd  REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service         TEXT NOT NULL,
    slack_user_id   TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(service, slack_user_id)
);

CREATE TABLE IF NOT EXISTS service_settings (
    service TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (service, key)
);

CREATE INDEX IF NOT EXISTS idx_error_groups_status ON error_groups(status);
CREATE INDEX IF NOT EXISTS idx_error_groups_service ON error_groups(service);
CREATE INDEX IF NOT EXISTS idx_investigations_fingerprint ON investigations(fingerprint);
CREATE INDEX IF NOT EXISTS idx_investigations_status ON investigations(status);
CREATE INDEX IF NOT EXISTS idx_run_log_started ON run_log(started_at);
"""

# Defaults for settings that can be toggled from the dashboard
SETTINGS_DEFAULTS: dict[str, str] = {
    "investigations_enabled": "true",
    # Opt-in: a service only gets code changes once it is explicitly enabled,
    # globally here and per-service on the Services tab.
    "fixes_enabled": "false",
    "slack_enabled": "true",
    # Documenting and issue-filing are separate stages: a deployment whose
    # tracker issue already carries the full RCA can turn the doc sink off
    # without losing the write-up, and a doc-only deployment can file nothing.
    "docs_enabled": "true",
    "issues_enabled": "true",
    "max_errors_per_run": "10",
    "max_investigations_per_day": "50",
    "investigation_budget": "2.00",
}

# Settings that can be overridden per-service
PER_SERVICE_KEYS = {
    "investigations_enabled",
    "fixes_enabled",
    "slack_enabled",
    "docs_enabled",
    "issues_enabled",
    "investigation_budget",
}


class ErrorCache:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # Migrations for existing databases
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply schema migrations to existing databases."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(investigations)").fetchall()}
        if "pr_status" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN pr_status TEXT DEFAULT ''")
            # Backfill: any investigation with a pr_url gets 'open'
            conn.execute("UPDATE investigations SET pr_status = 'open' WHERE pr_url != '' AND pr_url IS NOT NULL")
        if "is_duplicate" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN is_duplicate INTEGER DEFAULT 0")
        if "duplicate_of_url" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN duplicate_of_url TEXT DEFAULT ''")
        if "failure_reason" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN failure_reason TEXT DEFAULT ''")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- Settings --

    def get_setting(self, key: str) -> str:
        """Get a setting value, falling back to SETTINGS_DEFAULTS."""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else SETTINGS_DEFAULTS.get(key, "")

    def get_all_settings(self) -> dict[str, str]:
        """Get all settings, merged with defaults."""
        settings = dict(SETTINGS_DEFAULTS)
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            for row in rows:
                settings[row["key"]] = row["value"]
        return settings

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

    # -- Per-service settings --

    def get_service_settings(self, service: str) -> dict[str, str]:
        """Get per-service overrides (only keys in PER_SERVICE_KEYS)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM service_settings WHERE service = ?",
                (service,),
            ).fetchall()
            return {r["key"]: r["value"] for r in rows}

    def get_all_service_settings(self) -> dict[str, dict[str, str]]:
        """Get all per-service overrides, keyed by service name."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT service, key, value FROM service_settings ORDER BY service"
            ).fetchall()
        result: dict[str, dict[str, str]] = {}
        for r in rows:
            result.setdefault(r["service"], {})[r["key"]] = r["value"]
        return result

    def set_service_setting(self, service: str, key: str, value: str) -> None:
        if key not in PER_SERVICE_KEYS:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO service_settings (service, key, value) VALUES (?, ?, ?)",
                (service, key, value),
            )

    def delete_service_setting(self, service: str, key: str) -> None:
        """Remove a per-service override (reverts to global default)."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM service_settings WHERE service = ? AND key = ?",
                (service, key),
            )

    def resolve_service_settings(self, service: str) -> dict[str, str]:
        """Get effective settings for a service: global defaults → global overrides → per-service."""
        settings = self.get_all_settings()
        overrides = self.get_service_settings(service)
        settings.update(overrides)
        return settings

    def is_known(self, fingerprint: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM error_groups WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return row is not None

    def count_investigations_today(self) -> int:
        """Count investigations started today (UTC)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM investigations WHERE date(started_at) = date('now')"
            ).fetchone()
            return row[0]

    def get_recent_investigations_for_service(self, service: str, limit: int = 10) -> list[dict]:
        """Get recent completed investigations for a service, for dedup context."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT i.fingerprint, i.root_cause, i.linear_issue_url, i.pr_url,
                          i.severity, e.message_template
                   FROM investigations i
                   JOIN error_groups e ON i.fingerprint = e.fingerprint
                   WHERE e.service = ? AND i.status = 'complete'
                   ORDER BY i.started_at DESC LIMIT ?""",
                (service, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_status(self, fingerprint: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM error_groups WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return row["status"] if row else None

    def upsert_error(self, group: ErrorGroup) -> bool:
        """Insert or update an error group. Returns True if this is a new error."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT fingerprint, occurrence_count FROM error_groups WHERE fingerprint = ?",
                (group.fingerprint,),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE error_groups
                       SET last_seen = ?, occurrence_count = occurrence_count + ?,
                           sample_log_json = ?
                       WHERE fingerprint = ?""",
                    (
                        group.last_seen.isoformat(),
                        group.occurrence_count,
                        group.sample_log_json(),
                        group.fingerprint,
                    ),
                )
                return False

            conn.execute(
                """INSERT INTO error_groups
                   (fingerprint, service, error_type, message_template,
                    first_seen, last_seen, occurrence_count, status, region,
                    sample_log_json, dd_query_link)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    group.fingerprint,
                    group.service,
                    group.error_type,
                    group.message_template,
                    group.first_seen.isoformat(),
                    group.last_seen.isoformat(),
                    group.occurrence_count,
                    group.status,
                    group.region,
                    group.sample_log_json(),
                    group.dd_query_link,
                ),
            )
            return True

    def mark_investigating(self, fingerprint: str, investigation_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE error_groups SET status = 'investigating' WHERE fingerprint = ?",
                (fingerprint,),
            )

    def save_investigation(self, inv: Investigation) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO investigations
                   (id, fingerprint, started_at, completed_at, root_cause,
                    is_actionable, is_fixable, is_duplicate, duplicate_of_url,
                    estimated_fix_lines, severity,
                    affected_files, fix_description, recommendation,
                    notion_page_url, linear_issue_url, linear_issue_id,
                    pr_url, branch_name, fix_error, claude_cost_usd,
                    claude_input_tokens, claude_output_tokens, status, pr_status,
                    failure_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    inv.id,
                    inv.fingerprint,
                    inv.started_at.isoformat(),
                    inv.completed_at.isoformat() if inv.completed_at else None,
                    inv.root_cause,
                    int(inv.is_actionable),
                    int(inv.is_fixable),
                    int(inv.is_duplicate),
                    inv.duplicate_of_url,
                    inv.estimated_fix_lines,
                    inv.severity,
                    json.dumps(inv.affected_files),
                    inv.fix_description,
                    inv.recommendation,
                    inv.notion_page_url,
                    inv.linear_issue_url,
                    inv.linear_issue_id,
                    inv.pr_url,
                    inv.branch_name,
                    inv.fix_error,
                    inv.claude_cost_usd,
                    inv.claude_input_tokens,
                    inv.claude_output_tokens,
                    inv.status,
                    inv.pr_status,
                    inv.failure_reason,
                ),
            )

    def get_open_prs(self) -> list[dict[str, Any]]:
        """Get investigations with PRs that haven't been merged or closed yet."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT i.id, i.fingerprint, i.pr_url, i.linear_issue_url,
                          i.notion_page_url, e.service
                   FROM investigations i
                   JOIN error_groups e ON i.fingerprint = e.fingerprint
                   WHERE i.pr_url != '' AND i.pr_url IS NOT NULL
                     AND (i.pr_status IS NULL OR i.pr_status = '' OR i.pr_status = 'open')""",
            ).fetchall()
            return [dict(r) for r in rows]

    def update_pr_status(self, investigation_id: str, pr_status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE investigations SET pr_status = ? WHERE id = ?",
                (pr_status, investigation_id),
            )

    def update_error_status(self, fingerprint: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE error_groups SET status = ? WHERE fingerprint = ?",
                (status, fingerprint),
            )

    def log_run(
        self, run_id: str, started_at: datetime, completed_at: datetime, stats: dict[str, Any]
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO run_log
                   (run_id, started_at, completed_at, errors_fetched, new_errors,
                    investigated, issues_created, prs_created, total_cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    started_at.isoformat(),
                    completed_at.isoformat(),
                    stats.get("errors_fetched", 0),
                    stats.get("new_errors", 0),
                    stats.get("investigated", 0),
                    stats.get("issues_created", 0),
                    stats.get("prs_created", 0),
                    stats.get("total_cost_usd", 0.0),
                ),
            )

    # -- Dashboard query methods --

    def get_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM run_log ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_errors(
        self, service: str | None = None, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = """
            SELECT e.*,
                   i.root_cause AS inv_root_cause,
                   i.severity AS inv_severity,
                   i.notion_page_url AS inv_notion_url,
                   i.linear_issue_url AS inv_linear_url,
                   i.pr_url AS inv_pr_url,
                   i.pr_status AS inv_pr_status
            FROM error_groups e
            LEFT JOIN investigations i ON i.fingerprint = e.fingerprint
              AND i.id = (
                  SELECT id FROM investigations
                  WHERE fingerprint = e.fingerprint
                  ORDER BY started_at DESC LIMIT 1
              )
            WHERE 1=1"""
        params: list[Any] = []
        if service:
            query += " AND e.service = ?"
            params.append(service)
        if status:
            query += " AND e.status = ?"
            params.append(status)
        query += " ORDER BY e.last_seen DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_related_errors(self) -> dict[str, list[str]]:
        """Group fingerprints by shared Linear issue URL (same root cause).

        Returns {linear_issue_url: [fingerprint, ...]} for URLs with 2+ errors.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT i.linear_issue_url, e.fingerprint, e.message_template
                   FROM investigations i
                   JOIN error_groups e ON i.fingerprint = e.fingerprint
                   WHERE i.linear_issue_url IS NOT NULL AND i.linear_issue_url != ''
                     AND i.linear_issue_url LIKE '%linear.app%'
                   AND i.id = (
                       SELECT id FROM investigations
                       WHERE fingerprint = e.fingerprint
                       ORDER BY started_at DESC LIMIT 1
                   )
                   ORDER BY i.linear_issue_url"""
            ).fetchall()

        groups: dict[str, list[dict[str, str]]] = {}
        for r in rows:
            url = r["linear_issue_url"]
            groups.setdefault(url, []).append({
                "fingerprint": r["fingerprint"],
                "message": r["message_template"] or "",
            })

        # Only return groups with 2+ errors
        return {url: members for url, members in groups.items() if len(members) > 1}

    def get_error_detail(self, fingerprint: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            error = conn.execute(
                "SELECT * FROM error_groups WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if not error:
                return None
            investigations = conn.execute(
                "SELECT * FROM investigations WHERE fingerprint = ? ORDER BY started_at DESC",
                (fingerprint,),
            ).fetchall()
            return {
                "error": dict(error),
                "investigations": [dict(r) for r in investigations],
            }

    def get_investigations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT i.*, e.service, e.error_type
                   FROM investigations i
                   JOIN error_groups e ON i.fingerprint = e.fingerprint
                   ORDER BY i.started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM error_groups").fetchone()[0]
            by_status = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM error_groups GROUP BY status"
            ).fetchall()
            by_service = conn.execute(
                "SELECT service, COUNT(*) as cnt FROM error_groups GROUP BY service"
            ).fetchall()
            total_cost = conn.execute(
                "SELECT COALESCE(SUM(total_cost_usd), 0) FROM run_log WHERE started_at > datetime('now', '-7 days')"
            ).fetchone()[0]
            daily_costs = conn.execute(
                """SELECT date(started_at) as day,
                          SUM(total_cost_usd) as cost,
                          SUM(investigated) as investigations,
                          SUM(prs_created) as fixes
                   FROM run_log
                   WHERE started_at > datetime('now', '-30 days')
                   GROUP BY date(started_at)
                   ORDER BY day"""
            ).fetchall()

            return {
                "total_errors": total,
                "by_status": {r["status"]: r["cnt"] for r in by_status},
                "by_service": {r["service"]: r["cnt"] for r in by_service},
                "total_cost_7d": total_cost,
                "daily_costs": [dict(r) for r in daily_costs],
            }

    # -- Subscription methods --

    def get_subscriptions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions ORDER BY service, display_name"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_subscribers_for_service(self, service: str) -> list[str]:
        """Return list of Slack user IDs subscribed to a service."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT slack_user_id FROM subscriptions WHERE service = ?",
                (service,),
            ).fetchall()
            return [r["slack_user_id"] for r in rows]

    def add_subscription(self, service: str, slack_user_id: str, display_name: str) -> bool:
        """Add a subscription. Returns False if it already exists."""
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO subscriptions (service, slack_user_id, display_name) VALUES (?, ?, ?)",
                    (service, slack_user_id, display_name),
                )
                return True
            except Exception:
                return False

    def remove_subscription(self, subscription_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
