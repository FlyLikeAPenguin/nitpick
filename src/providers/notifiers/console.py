from __future__ import annotations

import logging
from typing import Any

from ...models import ErrorGroup, Investigation

log = logging.getLogger(__name__)


class ConsoleNotifier:
    """Logs investigation results to stdout -- zero-SaaS default."""

    @property
    def is_configured(self) -> bool:
        return True

    def notify(
        self,
        error: ErrorGroup,
        investigation: Investigation,
        config: Any,
        subscribers: list[str] | None = None,
    ) -> None:
        summary = investigation.root_cause[:80] if investigation.root_cause else error.error_type
        line = f"[{investigation.severity.upper()}] {error.service}: {summary}"

        links = []
        if investigation.notion_page_url:
            links.append(f"Notion: {investigation.notion_page_url}")
        if investigation.linear_issue_url:
            links.append(f"Linear: {investigation.linear_issue_url}")
        if investigation.pr_url:
            links.append(f"PR: {investigation.pr_url}")

        if links:
            line += " | " + " | ".join(links)

        log.info(line)

    def notify_daily_limit(self, count: int, limit: int) -> None:
        log.warning(
            "Daily investigation limit reached: %d/%d. New errors will be queued.",
            count,
            limit,
        )
