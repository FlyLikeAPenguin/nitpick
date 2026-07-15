"""Provider factory functions.

Instantiate concrete providers from a Config object. The ``config.providers``
dict (parsed from the ``providers:`` section of config.yml) selects the
implementation. When the section is absent every factory falls back to a
sensible default (Datadog / Markdown / GitHub / Console).
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .base import DocSink, ErrorSource, IssueTracker, Notifier

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)

__all__ = [
    "build_source",
    "build_doc_sink",
    "build_tracker",
    "build_notifier",
    "ErrorSource",
    "DocSink",
    "IssueTracker",
    "Notifier",
]


def _providers_block(config: Config) -> dict:
    """Return the ``providers`` dict from config, defaulting to empty."""
    return getattr(config, "providers", None) or {}


# ------------------------------------------------------------------
# ErrorSource
# ------------------------------------------------------------------

def build_source(config: Config, region: str = "eu") -> ErrorSource:
    """Build a :class:`DatadogSource` for *region*.

    Credentials are read from env vars following the convention
    ``DD_{REGION_UPPER}_API_KEY`` / ``DD_{REGION_UPPER}_APP_KEY``.
    Falls back to the Config dataclass attributes for backward compat.
    """
    from .sources.datadog import DatadogSource

    providers = _providers_block(config)
    source_cfg = providers.get("source", {})
    site = source_cfg.get("site", "")

    region_upper = region.upper()

    # Try env var convention first, then fall back to Config attrs
    api_key = (
        os.getenv(f"DD_{region_upper}_API_KEY", "")
        or getattr(config, f"dd_{region}_api_key", "")
    )
    app_key = (
        os.getenv(f"DD_{region_upper}_APP_KEY", "")
        or getattr(config, f"dd_{region}_app_key", "")
    )

    return DatadogSource(region=region, api_key=api_key, app_key=app_key, site=site)


# ------------------------------------------------------------------
# DocSink
# ------------------------------------------------------------------

def build_doc_sink(config: Config) -> DocSink | None:
    """Build a doc sink.  Returns ``MarkdownDocSink`` by default,
    ``NotionDocSink`` when Notion keys are present and type is ``"notion"``.
    """
    providers = _providers_block(config)
    sink_cfg = providers.get("doc_sink", {})
    sink_type = sink_cfg.get("type", "markdown")

    if sink_type == "notion":
        api_key = getattr(config, "notion_api_key", "") or os.getenv("NOTION_API_KEY", "")
        db_id = getattr(config, "notion_database_id", "") or os.getenv("NOTION_DATABASE_ID", "")
        if api_key and db_id:
            from .docsinks.notion import NotionDocSink
            return NotionDocSink(api_key=api_key, database_id=db_id)
        log.warning("Notion doc sink requested but credentials missing; falling back to markdown")

    from .docsinks.markdown import MarkdownDocSink
    return MarkdownDocSink()


# ------------------------------------------------------------------
# IssueTracker
# ------------------------------------------------------------------

def build_tracker(config: Config) -> IssueTracker | None:
    """Build an issue tracker.  Returns ``GitHubTracker`` by default,
    ``LinearTracker`` when Linear keys are present and type is ``"linear"``.
    """
    providers = _providers_block(config)
    tracker_cfg = providers.get("tracker", {})
    tracker_type = tracker_cfg.get("type", "github")

    if tracker_type == "linear":
        api_key = getattr(config, "linear_api_key", "") or os.getenv("LINEAR_API_KEY", "")
        team_id = getattr(config, "linear_team_id", "") or os.getenv("LINEAR_TEAM_ID", "")
        if api_key and team_id:
            from .trackers.linear import LinearTracker
            return LinearTracker(api_key=api_key, team_id=team_id)
        log.warning("Linear tracker requested but credentials missing; falling back to GitHub")

    from .trackers.github import GitHubTracker
    repo = tracker_cfg.get("repo", "")
    return GitHubTracker(default_repo=repo)


# ------------------------------------------------------------------
# Notifier
# ------------------------------------------------------------------

def build_notifier(config: Config) -> Notifier | None:
    """Build a notifier.  Returns ``ConsoleNotifier`` by default,
    ``SlackNotifier`` when Slack keys are present and type is ``"slack"``.
    """
    providers = _providers_block(config)
    notifier_cfg = providers.get("notifier", {})
    notifier_type = notifier_cfg.get("type", "console")

    if notifier_type == "slack":
        webhook = getattr(config, "slack_webhook_url", "") or os.getenv("SLACK_WEBHOOK_URL", "")
        channel = getattr(config, "slack_channel_id", "") or os.getenv("SLACK_CHANNEL_ID", "")
        if webhook:
            from .notifiers.slack import SlackNotifier
            return SlackNotifier(webhook_url=webhook, channel_id=channel)
        log.warning("Slack notifier requested but webhook URL missing; falling back to console")

    from .notifiers.console import ConsoleNotifier
    return ConsoleNotifier()
