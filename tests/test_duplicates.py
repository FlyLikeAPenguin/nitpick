"""Tests for how an error whose root cause is already tracked is handled.

Fingerprint dedup only stops the *same* error being re-investigated. A second
error with a different fingerprint but the same root cause still reaches the
agent, and before this boundary existed it opened a second ticket every time —
even when the agent had correctly identified it as a duplicate. The recurrence
now lands on the tracked issue instead, and never opens a new one.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.cache import ErrorCache
from src.config import Config
from src.investigator import build_investigation_prompt, investigate
from src.main import recurrence_comment, run_pipeline
from src.models import CodePath, ErrorGroup, Investigation, ServiceConfig

SERVICE = ServiceConfig(
    name="payments",
    datadog_query="service:payments status:error",
    regions=["us"],
    code_paths=[CodePath(repo="core", path="src")],
)

TRACKED_ISSUE = "https://tracker.example/issue/ABC-100"


def error_group(fingerprint: str = "abc123") -> ErrorGroup:
    now = datetime.now(timezone.utc)
    return ErrorGroup(
        fingerprint=fingerprint,
        service="payments",
        error_type="TimeoutError",
        message_template="request <ID> timed out",
        region="us",
        first_seen=now,
        last_seen=now,
        occurrence_count=2,
    )


def claude_envelope(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "result": json.dumps(payload),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0.01,
        }
    )


def run_investigation(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> Investigation:
    """Run investigate() with the Claude CLI replaced by a canned response."""

    class Completed:
        returncode = 0
        stdout = claude_envelope(payload)
        stderr = ""

    monkeypatch.setattr("src.investigator.subprocess.run", lambda *a, **kw: Completed())
    return investigate(error_group(), SERVICE, Config())


class TestInvestigationContract:
    def test_prompt_asks_for_the_duplicate_fields(self) -> None:
        prompt = build_investigation_prompt(error_group(), SERVICE, Config())

        assert "is_duplicate" in prompt
        assert "duplicate_of_url" in prompt

    def test_prompt_keeps_duplicate_independent_of_actionable(self) -> None:
        prompt = build_investigation_prompt(error_group(), SERVICE, Config())

        assert "is_duplicate is independent of is_actionable" in prompt

    def test_duplicate_records_the_tracked_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        investigation = run_investigation(
            monkeypatch,
            {"root_cause": "rc", "is_actionable": True, "is_duplicate": True,
             "duplicate_of_url": TRACKED_ISSUE},
        )

        assert investigation.is_duplicate is True
        assert investigation.duplicate_of_url == TRACKED_ISSUE

    def test_duplicate_url_is_trimmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        investigation = run_investigation(
            monkeypatch,
            {"root_cause": "rc", "is_actionable": True, "is_duplicate": True,
             "duplicate_of_url": f"  {TRACKED_ISSUE}  "},
        )

        assert investigation.duplicate_of_url == TRACKED_ISSUE

    def test_duplicate_without_an_issue_url_is_not_a_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        investigation = run_investigation(
            monkeypatch,
            {"root_cause": "rc", "is_actionable": True, "is_duplicate": True,
             "duplicate_of_url": ""},
        )

        assert investigation.is_duplicate is False

    def test_agent_cannot_set_the_filed_issue_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        investigation = run_investigation(
            monkeypatch,
            {"root_cause": "rc", "is_actionable": True,
             "linear_issue_url": "https://tracker.example/issue/MADE-UP"},
        )

        assert investigation.linear_issue_url == ""


class TestPersistence:
    def test_duplicate_flags_survive_a_round_trip(self, tmp_path: Path) -> None:
        cache = ErrorCache(tmp_path / "cache.db")
        cache.upsert_error(error_group())
        cache.save_investigation(
            Investigation(
                id="inv-1", fingerprint="abc123", started_at=datetime.now(timezone.utc),
                is_duplicate=True, duplicate_of_url=TRACKED_ISSUE, status="complete",
            )
        )

        with sqlite3.connect(tmp_path / "cache.db") as conn:
            row = conn.execute(
                "SELECT is_duplicate, duplicate_of_url FROM investigations WHERE id = 'inv-1'"
            ).fetchone()

        assert row == (1, TRACKED_ISSUE)

    def test_database_predating_the_columns_is_migrated(self, tmp_path: Path) -> None:
        db = tmp_path / "cache.db"
        ErrorCache(db)
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE investigations DROP COLUMN is_duplicate")
            conn.execute("ALTER TABLE investigations DROP COLUMN duplicate_of_url")

        cache = ErrorCache(db)
        cache.upsert_error(error_group())
        cache.save_investigation(
            Investigation(
                id="inv-1", fingerprint="abc123", started_at=datetime.now(timezone.utc),
                is_duplicate=True, duplicate_of_url=TRACKED_ISSUE, status="complete",
            )
        )

        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT is_duplicate, duplicate_of_url FROM investigations WHERE id = 'inv-1'"
            ).fetchone()

        assert row == (1, TRACKED_ISSUE)


class TestRecurrenceComment:
    def test_comment_identifies_the_recurring_error(self) -> None:
        group = error_group()
        group.dd_query_link = "https://logs.example/query"

        body = recurrence_comment(group, Investigation(
            id="inv-1", fingerprint=group.fingerprint,
            started_at=datetime.now(timezone.utc), root_cause="connection reset",
        ))

        assert "abc123" in body
        assert "2 occurrences" in body or "Occurrences: 2" in body
        assert "https://logs.example/query" in body
        assert "connection reset" in body


class FakeTracker:
    is_configured = True

    def __init__(self) -> None:
        self.created: list[ErrorGroup] = []
        self.comments: list[tuple[str, str]] = []

    def create(self, error: ErrorGroup, investigation: Investigation) -> str:
        self.created.append(error)
        return f"https://tracker.example/issue/NEW-{len(self.created)}"

    def comment(self, issue_ref: str, body: str) -> None:
        self.comments.append((issue_ref, body))

    def attach_pr(self, issue_ref: str, pr_url: str, pr_title: str) -> None:
        pass

    def transition(self, issue_ref: str, state: str) -> None:
        pass


class FakeNotifier:
    is_configured = True

    def __init__(self) -> None:
        self.sent: list[ErrorGroup] = []

    def notify(self, error, investigation, config, subscribers=None) -> None:
        self.sent.append(error)

    def notify_daily_limit(self, count: int, limit: int) -> None:
        pass


class FakeSource:
    is_configured = True

    def fetch(self, query: str, lookback_seconds: int, limit: int) -> list[dict[str, Any]]:
        return [
            {"attributes": {"timestamp": "2026-01-01T00:00:00Z",
                            "attributes": {"error": {"kind": "TimeoutError",
                                                     "message": "request 1234 timed out"}}}},
        ]


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run the pipeline against fakes, with a canned investigation result."""
    tracker, notifier = FakeTracker(), FakeNotifier()
    monkeypatch.setattr("src.main.build_source", lambda config, region: FakeSource())
    monkeypatch.setattr("src.main.build_tracker", lambda config: tracker)
    monkeypatch.setattr("src.main.build_notifier", lambda config: notifier)
    monkeypatch.setattr("src.main.build_doc_sink", lambda config: None)
    monkeypatch.setattr("src.main.cleanup_stale_worktrees", lambda config: None)
    monkeypatch.setattr("src.main.time.sleep", lambda seconds: None)

    def run(investigation: Investigation) -> tuple[FakeTracker, FakeNotifier]:
        monkeypatch.setattr(
            "src.main.investigate",
            lambda group, svc_config, config, recent=None: investigation,
        )
        config = Config(
            db_path=tmp_path / "cache.db",
            log_dir=tmp_path,
            services={"payments": SERVICE},
        )
        run_pipeline(config)
        return tracker, notifier

    return run


def investigation_result(**overrides: Any) -> Investigation:
    defaults: dict[str, Any] = {
        "id": "inv-1", "fingerprint": "unused",
        "started_at": datetime.now(timezone.utc),
        "root_cause": "connection reset by peer",
        "is_actionable": True, "is_fixable": False, "status": "complete",
    }
    return Investigation(**{**defaults, **overrides})


class TestPipelineRouting:
    def test_untracked_error_opens_an_issue(self, pipeline) -> None:
        tracker, _ = pipeline(investigation_result())

        assert len(tracker.created) == 1
        assert tracker.comments == []

    def test_already_tracked_error_opens_no_issue(self, pipeline) -> None:
        tracker, _ = pipeline(
            investigation_result(is_duplicate=True, duplicate_of_url=TRACKED_ISSUE)
        )

        assert tracker.created == []

    def test_already_tracked_error_is_recorded_on_the_tracked_issue(self, pipeline) -> None:
        tracker, _ = pipeline(
            investigation_result(is_duplicate=True, duplicate_of_url=TRACKED_ISSUE)
        )

        assert len(tracker.comments) == 1
        issue_ref, body = tracker.comments[0]
        assert issue_ref == TRACKED_ISSUE
        assert "connection reset by peer" in body

    def test_already_tracked_error_does_not_notify_again(self, pipeline) -> None:
        _, notifier = pipeline(
            investigation_result(is_duplicate=True, duplicate_of_url=TRACKED_ISSUE)
        )

        assert notifier.sent == []

    def test_untracked_error_still_notifies(self, pipeline) -> None:
        _, notifier = pipeline(investigation_result())

        assert len(notifier.sent) == 1

    def test_error_needing_no_action_does_nothing(self, pipeline) -> None:
        tracker, notifier = pipeline(investigation_result(is_actionable=False))

        assert tracker.created == []
        assert tracker.comments == []
        assert notifier.sent == []
