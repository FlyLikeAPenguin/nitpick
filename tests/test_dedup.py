"""Tests for the SQLite dedup cache and settings resolution.

Deduplication is what stops a recurring error being re-investigated on every
run, and the settings layer is what keeps fix PRs off unless explicitly
enabled. Both are cost and safety boundaries, so they are covered directly.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.cache import ErrorCache
from src.models import ErrorGroup


@pytest.fixture
def cache(tmp_path: Path) -> ErrorCache:
    return ErrorCache(tmp_path / "cache.db")


def group(fingerprint: str = "abc123", *, service: str = "payments", count: int = 1) -> ErrorGroup:
    now = datetime.now(timezone.utc)
    return ErrorGroup(
        fingerprint=fingerprint,
        service=service,
        error_type="TimeoutError",
        message_template="request <ID> timed out",
        region="us",
        first_seen=now,
        last_seen=now,
        occurrence_count=count,
    )


class TestDeduplication:
    def test_first_sighting_is_reported_as_new(self, cache: ErrorCache) -> None:
        assert cache.upsert_error(group()) is True

    def test_second_sighting_is_not_new(self, cache: ErrorCache) -> None:
        cache.upsert_error(group())

        assert cache.upsert_error(group()) is False

    def test_occurrences_accumulate_across_runs(self, cache: ErrorCache) -> None:
        cache.upsert_error(group(count=3))
        cache.upsert_error(group(count=5))

        errors = cache.get_errors()

        assert errors[0]["occurrence_count"] == 8

    def test_distinct_fingerprints_are_tracked_separately(self, cache: ErrorCache) -> None:
        cache.upsert_error(group("aaa"))
        cache.upsert_error(group("bbb"))

        assert len(cache.get_errors()) == 2

    def test_unseen_fingerprint_is_not_known(self, cache: ErrorCache) -> None:
        assert cache.is_known("never-seen") is False

    def test_stored_fingerprint_is_known(self, cache: ErrorCache) -> None:
        cache.upsert_error(group())

        assert cache.is_known("abc123") is True

    def test_new_errors_start_in_new_status(self, cache: ErrorCache) -> None:
        cache.upsert_error(group())

        assert cache.get_status("abc123") == "new"

    def test_status_can_be_advanced(self, cache: ErrorCache) -> None:
        cache.upsert_error(group())
        cache.update_error_status("abc123", "documented")

        assert cache.get_status("abc123") == "documented"

    def test_marking_investigating_updates_status(self, cache: ErrorCache) -> None:
        cache.upsert_error(group())
        cache.mark_investigating("abc123", "inv-1")

        assert cache.get_status("abc123") == "investigating"

    def test_status_of_unknown_fingerprint_is_none(self, cache: ErrorCache) -> None:
        assert cache.get_status("never-seen") is None


class TestErrorFiltering:
    def test_errors_filter_by_service(self, cache: ErrorCache) -> None:
        cache.upsert_error(group("aaa", service="payments"))
        cache.upsert_error(group("bbb", service="billing"))

        results = cache.get_errors(service="payments")

        assert [e["fingerprint"] for e in results] == ["aaa"]

    def test_errors_filter_by_status(self, cache: ErrorCache) -> None:
        cache.upsert_error(group("aaa"))
        cache.upsert_error(group("bbb"))
        cache.update_error_status("aaa", "fixed")

        results = cache.get_errors(status="fixed")

        assert [e["fingerprint"] for e in results] == ["aaa"]

    def test_limit_caps_returned_rows(self, cache: ErrorCache) -> None:
        for i in range(5):
            cache.upsert_error(group(f"fp{i}"))

        assert len(cache.get_errors(limit=2)) == 2


class TestSettingsResolution:
    def test_unset_service_inherits_global_settings(self, cache: ErrorCache) -> None:
        cache.set_setting("fixes_enabled", "true")

        resolved = cache.resolve_service_settings("payments")

        assert resolved["fixes_enabled"] == "true"

    def test_service_setting_overrides_global(self, cache: ErrorCache) -> None:
        cache.set_setting("fixes_enabled", "true")
        cache.set_service_setting("payments", "fixes_enabled", "false")

        assert cache.resolve_service_settings("payments")["fixes_enabled"] == "false"

    def test_override_is_scoped_to_its_service(self, cache: ErrorCache) -> None:
        cache.set_setting("fixes_enabled", "true")
        cache.set_service_setting("payments", "fixes_enabled", "false")

        assert cache.resolve_service_settings("billing")["fixes_enabled"] == "true"

    def test_deleting_an_override_restores_the_global_value(self, cache: ErrorCache) -> None:
        cache.set_setting("fixes_enabled", "true")
        cache.set_service_setting("payments", "fixes_enabled", "false")
        cache.delete_service_setting("payments", "fixes_enabled")

        assert cache.resolve_service_settings("payments")["fixes_enabled"] == "true"

    def test_fixes_stay_disabled_for_a_service_with_no_settings(self, cache: ErrorCache) -> None:
        """The safety-critical default: a brand-new service never auto-fixes."""
        assert cache.resolve_service_settings("brand-new")["fixes_enabled"] == "false"


class TestRunLog:
    def test_runs_are_recorded_with_their_stats(self, cache: ErrorCache) -> None:
        now = datetime.now(timezone.utc)
        cache.log_run("run-1", now, now, {"errors_fetched": 40, "investigated": 3})

        runs = cache.get_runs()

        assert len(runs) == 1
        assert runs[0]["errors_fetched"] == 40
        assert runs[0]["investigated"] == 3

    def test_missing_stats_default_to_zero(self, cache: ErrorCache) -> None:
        now = datetime.now(timezone.utc)
        cache.log_run("run-1", now, now, {})

        assert cache.get_runs()[0]["total_cost_usd"] == 0.0

    def test_no_investigations_recorded_today_on_a_fresh_cache(self, cache: ErrorCache) -> None:
        assert cache.count_investigations_today() == 0
