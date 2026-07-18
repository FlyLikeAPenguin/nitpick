"""Seed a credential-free dashboard demo with deterministic sample data."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .cache import ErrorCache
from .config import Config
from .models import ErrorGroup, Investigation

DEMO_DB_NAME = "demo_error_cache.db"
DEMO_RUN_ID = "demo-run-001"


def demo_config(config: Config) -> Config:
    """Return a config that points the dashboard at the isolated demo database."""
    return replace(config, db_path=config.db_path.with_name(DEMO_DB_NAME))


def seed_demo_data(db_path: Path) -> None:
    """Create a small, repeatable dashboard dataset without external services."""
    cache = ErrorCache(db_path)
    now = datetime.now(UTC).replace(microsecond=0)

    groups = [
        ErrorGroup(
            fingerprint="demo-checkout-timeout",
            service="checkout-api",
            error_type="TimeoutError",
            message_template="Payment provider timed out after <ID>ms",
            region="us",
            first_seen=now - timedelta(hours=3),
            last_seen=now - timedelta(minutes=8),
            occurrence_count=37,
            status="fixed",
            sample_log={"message": "Payment provider timed out after 30000ms"},
        ),
        ErrorGroup(
            fingerprint="demo-worker-decode",
            service="background-worker",
            error_type="JSONDecodeError",
            message_template="Expected value at line <ID> column <ID>",
            region="eu",
            first_seen=now - timedelta(hours=1),
            last_seen=now - timedelta(minutes=22),
            occurrence_count=12,
            status="documented",
            sample_log={"message": "Expected value at line 1 column 2"},
        ),
        ErrorGroup(
            fingerprint="demo-health-check",
            service="catalog-api",
            error_type="ConnectionResetError",
            message_template="Health-check connection reset by peer",
            region="us",
            first_seen=now - timedelta(minutes=45),
            last_seen=now - timedelta(minutes=41),
            occurrence_count=4,
            status="new",
            sample_log={"message": "Health-check connection reset by peer"},
        ),
    ]
    for group in groups:
        cache.upsert_error(group)
        cache.update_error_status(group.fingerprint, group.status)

    investigations = [
        Investigation(
            id=str(uuid5(NAMESPACE_URL, "nitpick-demo-checkout-timeout")),
            fingerprint="demo-checkout-timeout",
            started_at=now - timedelta(hours=2, minutes=55),
            completed_at=now - timedelta(hours=2, minutes=54),
            root_cause="The payment client uses a 30-second timeout while the provider regularly responds more slowly during peak traffic.",
            is_actionable=True,
            is_fixable=True,
            estimated_fix_lines=8,
            severity="high",
            affected_files=["src/payments/client.py"],
            fix_description="Use the configured provider timeout and retry transient gateway timeouts.",
            recommendation="Review the generated fix PR and add a provider-timeout regression test.",
            claude_cost_usd=0.18,
            claude_input_tokens=2200,
            claude_output_tokens=540,
            status="complete",
            pr_status="merged",
        ),
        Investigation(
            id=str(uuid5(NAMESPACE_URL, "nitpick-demo-worker-decode")),
            fingerprint="demo-worker-decode",
            started_at=now - timedelta(minutes=58),
            completed_at=now - timedelta(minutes=57),
            root_cause="A partner payload occasionally arrives empty during a retry window.",
            is_actionable=True,
            is_fixable=False,
            severity="medium",
            affected_files=["src/worker/decoder.py"],
            recommendation="Treat empty retry payloads as a deferred message and alert if the rate increases.",
            claude_cost_usd=0.11,
            claude_input_tokens=1500,
            claude_output_tokens=390,
            status="complete",
        ),
    ]
    for investigation in investigations:
        cache.save_investigation(investigation)

    if not any(run["run_id"] == DEMO_RUN_ID for run in cache.get_runs()):
        cache.log_run(
            DEMO_RUN_ID,
            now - timedelta(hours=3),
            now - timedelta(hours=2, minutes=54),
            {
                "errors_fetched": 53,
                "new_errors": 3,
                "investigated": 2,
                "issues_created": 2,
                "prs_created": 1,
                "total_cost_usd": 0.29,
            },
        )
