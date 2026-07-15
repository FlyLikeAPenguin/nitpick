"""Track the status of PRs created by the fixer.

Each pipeline run checks open PRs via `gh pr view` and updates the DB,
issue tracker, and RCA documentation when a PR is merged or closed.
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from .cache import ErrorCache
from .config import Config
from .providers.base import DocSink, IssueTracker

log = logging.getLogger(__name__)


def check_open_prs(
    cache: ErrorCache,
    config: Config,
    tracker: IssueTracker | None = None,
    doc_sink: DocSink | None = None,
) -> None:
    """Check all open PRs and update their status."""
    open_prs = cache.get_open_prs()
    if not open_prs:
        return

    log.info("Checking status of %d open PR(s)", len(open_prs))

    for pr in open_prs:
        pr_url = pr["pr_url"]
        status = _get_pr_status(pr_url)
        if not status:
            continue

        state = status.get("state", "").upper()

        if state == "MERGED":
            log.info("PR merged: %s", pr_url)
            cache.update_pr_status(pr["id"], "merged")
            cache.update_error_status(pr["fingerprint"], "fixed")

            if tracker and pr.get("linear_issue_url"):
                try:
                    tracker.transition(pr["linear_issue_url"], "done")
                except Exception as e:
                    log.warning("Tracker transition failed: %s", e)

            if doc_sink and pr.get("notion_page_url"):
                try:
                    doc_sink.comment(pr["notion_page_url"], f"Fix merged: {pr_url}")
                except Exception as e:
                    log.warning("Doc sink comment failed: %s", e)

        elif state == "CLOSED":
            log.info("PR closed without merge: %s", pr_url)
            cache.update_pr_status(pr["id"], "closed")
            cache.update_error_status(pr["fingerprint"], "documented")

            if doc_sink and pr.get("notion_page_url"):
                try:
                    doc_sink.comment(pr["notion_page_url"], f"Fix PR closed without merge: {pr_url}")
                except Exception as e:
                    log.warning("Doc sink comment failed: %s", e)


def _get_pr_status(pr_url: str) -> dict[str, Any] | None:
    """Get PR state via gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt,closedAt"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.warning("gh pr view failed for %s: %s", pr_url, result.stderr.strip()[:100])
            return None
        return json.loads(result.stdout)
    except Exception as e:
        log.warning("Failed to check PR status for %s: %s", pr_url, e)
        return None
