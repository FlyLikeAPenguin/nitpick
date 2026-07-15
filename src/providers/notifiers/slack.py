from __future__ import annotations

import logging
from typing import Any

import requests

from ...models import ErrorGroup, Investigation

log = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",      # orange circle
    "medium": "\U0001f7e1",    # yellow circle
    "low": "\U0001f7e2",       # green circle
}


class SlackNotifier:
    """Post investigation results to Slack via a webhook."""

    def __init__(self, webhook_url: str, channel_id: str = ""):
        self._webhook_url = webhook_url
        self._channel_id = channel_id

    @property
    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    def notify(
        self,
        error: ErrorGroup,
        investigation: Investigation,
        config: Any,
        subscribers: list[str] | None = None,
    ) -> None:
        if not self.is_configured:
            log.warning("No SLACK_WEBHOOK_URL configured, skipping notification")
            return

        emoji = SEVERITY_EMOJI.get(investigation.severity, "?")
        short_summary = error.error_type
        if investigation.root_cause:
            first_sentence = investigation.root_cause.split(".")[0].strip()
            if len(first_sentence) > 80:
                first_sentence = first_sentence[:77] + "..."
            short_summary = first_sentence
        title = f"{emoji} {investigation.severity.upper()}: {error.service} -- {short_summary}"

        links = []
        if investigation.notion_page_url:
            links.append(f"<{investigation.notion_page_url}|Notion RCA>")
        if investigation.linear_issue_url:
            links.append(f"<{investigation.linear_issue_url}|Linear Issue>")
        if investigation.pr_url:
            links.append(f"<{investigation.pr_url}|Fix PR>")
        if error.dd_query_link:
            links.append(f"<{error.dd_query_link}|Datadog Logs>")
        links_text = " | ".join(links) if links else "No links"

        rca_preview = investigation.root_cause[:300]
        if len(investigation.root_cause) > 300:
            rca_preview += "..."

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title[:150]},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Service:* `{error.service}` ({error.region})\n"
                        f"*Occurrences:* {error.occurrence_count}\n"
                        f"*Root cause:* {rca_preview}"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": links_text},
            },
        ]

        if investigation.pr_url:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Automated fix submitted:* <{investigation.pr_url}|Review PR>",
                },
            })

        if subscribers:
            mentions = " ".join(f"<@{uid}>" for uid in subscribers)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": mentions},
            })

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Nitpick | Cost: ${investigation.claude_cost_usd:.4f}",
                },
            ],
        })

        try:
            resp = requests.post(
                self._webhook_url,
                json={"blocks": blocks},
                timeout=10,
            )
            resp.raise_for_status()
            log.info("Slack notification sent for %s", error.fingerprint)
        except requests.RequestException as e:
            log.warning("Slack notification failed: %s", e)

    def notify_daily_limit(self, count: int, limit: int) -> None:
        if not self.is_configured:
            return

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Warning: Daily limit reached"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{count}/{limit}* investigations used today.\n"
                        "New errors will be queued and investigated tomorrow.\n"
                        "Adjust `ERROR_WATCHER_MAX_INVESTIGATIONS_PER_DAY` to change the limit."
                    ),
                },
            },
        ]

        try:
            resp = requests.post(
                self._webhook_url,
                json={"blocks": blocks},
                timeout=10,
            )
            resp.raise_for_status()
            log.info("Slack daily limit alert sent")
        except requests.RequestException as e:
            log.warning("Slack daily limit alert failed: %s", e)
