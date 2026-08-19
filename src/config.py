from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import CodePath, ServiceConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # Datadog
    dd_eu_api_key: str = ""
    dd_eu_app_key: str = ""
    dd_us_api_key: str = ""
    dd_us_app_key: str = ""

    # Claude
    claude_bin: str = "claude"
    claude_model: str = "claude-opus-5"
    claude_effort: str = "high"
    # None = no dollar cap. --max-budget-usd only applies to API-key billing;
    # on a Claude subscription it is inert, so the flag is omitted entirely
    # unless CLAUDE_MAX_BUDGET is set.
    claude_max_budget: float | None = None

    # Linear
    linear_api_key: str = ""
    linear_team_id: str = ""

    # Notion
    notion_api_key: str = ""
    notion_database_id: str = ""

    # Slack
    slack_channel_id: str = ""
    slack_webhook_url: str = ""

    # Dashboard
    dashboard_port: int = 8111

    # Tuning
    interval: int = 1800
    lookback: int = 3600
    max_errors_per_run: int = 10
    max_investigations_per_day: int = 50
    dry_run: bool = False

    # Parsed from config.yml
    providers: dict[str, dict] = field(default_factory=dict)
    services: dict[str, ServiceConfig] = field(default_factory=dict)
    repos: dict[str, str] = field(default_factory=dict)
    ignore_patterns: list[str] = field(default_factory=list)

    # Paths
    db_path: Path = PROJECT_ROOT / "data" / "error_cache.db"
    log_dir: Path = PROJECT_ROOT / "logs"
    static_dir: Path = PROJECT_ROOT / "static"


def _optional_float(raw: str | None) -> float | None:
    """Parse an optional numeric env var. Empty or unset means "no value"."""
    if raw is None or not raw.strip():
        return None
    return float(raw)


def load_config(env_path: Path | None = None, config_path: Path | None = None) -> Config:
    env_path = env_path or PROJECT_ROOT / ".env"
    config_path = config_path or PROJECT_ROOT / "config.yml"

    load_dotenv(env_path)

    cfg = Config(
        dd_eu_api_key=os.getenv("DD_EU_API_KEY", ""),
        dd_eu_app_key=os.getenv("DD_EU_APP_KEY", ""),
        dd_us_api_key=os.getenv("DD_US_API_KEY", ""),
        dd_us_app_key=os.getenv("DD_US_APP_KEY", ""),
        claude_bin=os.getenv("CLAUDE_BIN", "claude"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5"),
        claude_effort=os.getenv("CLAUDE_EFFORT", "high"),
        claude_max_budget=_optional_float(os.getenv("CLAUDE_MAX_BUDGET")),
        linear_api_key=os.getenv("LINEAR_API_KEY", ""),
        linear_team_id=os.getenv("LINEAR_TEAM_ID", ""),
        notion_api_key=os.getenv("NOTION_API_KEY", ""),
        notion_database_id=os.getenv("NOTION_DATABASE_ID", ""),
        slack_channel_id=os.getenv("SLACK_CHANNEL_ID", ""),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", ""),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8111")),
        interval=int(os.getenv("NITPICK_INTERVAL", os.getenv("ERROR_WATCHER_INTERVAL", "1800"))),
        lookback=int(os.getenv("NITPICK_LOOKBACK", os.getenv("ERROR_WATCHER_LOOKBACK", "3600"))),
        max_errors_per_run=int(os.getenv("NITPICK_MAX_ERRORS_PER_RUN", os.getenv("ERROR_WATCHER_MAX_ERRORS_PER_RUN", "10"))),
        max_investigations_per_day=int(os.getenv("NITPICK_MAX_INVESTIGATIONS_PER_DAY", os.getenv("ERROR_WATCHER_MAX_INVESTIGATIONS_PER_DAY", "50"))),
        dry_run=os.getenv("NITPICK_DRY_RUN", os.getenv("ERROR_WATCHER_DRY_RUN", "false")).lower() == "true",
    )

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

        cfg.providers = raw.get("providers", {})
        cfg.repos = raw.get("repos", {})
        cfg.ignore_patterns = raw.get("ignore_patterns", [])

        for name, svc in raw.get("services", {}).items():
            code_paths = [
                CodePath(repo=cp["repo"], path=cp["path"])
                for cp in svc.get("code_paths", [])
            ]
            cfg.services[name] = ServiceConfig(
                name=name,
                datadog_query=svc["datadog_query"],
                regions=svc.get("regions", ["eu", "us"]),
                code_paths=code_paths,
            )

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    return cfg


def validate_config(cfg: Config) -> list[str]:
    """Return list of missing required config items."""
    missing = []
    if not cfg.dd_eu_api_key and not cfg.dd_us_api_key:
        missing.append("At least one of DD_EU_API_KEY or DD_US_API_KEY")
    if not cfg.dd_eu_app_key and not cfg.dd_us_app_key:
        missing.append("At least one of DD_EU_APP_KEY or DD_US_APP_KEY")
    if not cfg.services:
        missing.append("No services configured in config.yml")
    return missing
