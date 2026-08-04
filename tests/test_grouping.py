"""Tests for grouping raw log entries into deduplicated error groups.

Grouping is the step that keeps investigation costs bounded: tens of thousands
of raw log entries collapse into a few hundred distinct errors, and only the
distinct ones reach the agent.
"""

from src.fingerprint import (
    extract_error_fields,
    extract_stack_signature,
    fingerprint_and_group,
    normalize_message,
)


def log(message: str, *, error_type: str = "TimeoutError", timestamp: str = "") -> dict:
    return {
        "attributes": {
            "timestamp": timestamp,
            "attributes": {"error": {"kind": error_type, "message": message}},
        }
    }


def test_repeated_errors_collapse_into_one_group() -> None:
    logs = [
        log("request 111111 timed out"),
        log("request 222222 timed out"),
        log("request 333333 timed out"),
    ]

    groups = fingerprint_and_group(logs, "payments", "us")

    assert len(groups) == 1
    assert groups[0].occurrence_count == 3


def test_distinct_errors_stay_separate() -> None:
    logs = [log("connection refused"), log("disk quota exceeded")]

    groups = fingerprint_and_group(logs, "payments", "us")

    assert len(groups) == 2


def test_groups_are_ordered_by_occurrence_count() -> None:
    logs = [log("rare failure")] + [log("common failure")] * 4

    groups = fingerprint_and_group(logs, "payments", "us")

    assert [g.occurrence_count for g in groups] == [4, 1]


def test_ignore_patterns_drop_matching_logs() -> None:
    logs = [log("health_check failed"), log("connection refused")]

    groups = fingerprint_and_group(logs, "payments", "us", ignore_patterns=["health_check"])

    assert len(groups) == 1
    assert "health_check" not in groups[0].message_template


def test_ignore_patterns_are_case_insensitive() -> None:
    logs = [log("DeprecationWarning: old API")]

    groups = fingerprint_and_group(logs, "payments", "us", ignore_patterns=["deprecationwarning"])

    assert groups == []


def test_tracer_noise_is_dropped_by_the_shipped_ignore_pattern() -> None:
    """Tracer transport failures are the agent's own output, not the service's.

    They are always transient and external, so investigating them costs a full
    agent run to reach a verdict that is known in advance.
    """
    logs = [
        log(
            '2026/01/02 03:04:05 Datadog Tracer v2.0.0 ERROR: lost 5 traces: '
            'Post "http://127.0.0.1:8126/v0.4/traces": context deadline exceeded '
            "(occurred: 02 Jan 26 03:04 UTC)"
        ),
        log(
            "2026/01/02 03:09:05 Datadog Tracer v2.0.0 ERROR: Error sending stats "
            "payload: Bad Gateway (occurred: 02 Jan 26 03:09 UTC)"
        ),
        log("connection refused"),
    ]

    groups = fingerprint_and_group(logs, "payments", "us", ignore_patterns=["Datadog Tracer"])

    assert len(groups) == 1
    assert groups[0].message_template == "connection refused"


def test_tracer_noise_fingerprints_apart_without_the_ignore_pattern() -> None:
    """Why an ignore pattern is required rather than better normalization.

    The endpoint path and the trailing "(occurred: ...)" clause both vary between
    occurrences of one underlying outage, and the clause uses a date format the
    leading-timestamp rule does not match — so the two collapse to distinct
    templates and each opens its own investigation.
    """
    logs = [
        log(
            '2026/01/02 03:04:05 Datadog Tracer v2.0.0 ERROR: lost 5 traces: '
            'Post "http://127.0.0.1:8126/v0.4/traces": context deadline exceeded '
            "(occurred: 02 Jan 26 03:04 UTC)"
        ),
        log(
            '2026/01/02 03:09:05 Datadog Tracer v2.0.0 ERROR: lost 9 traces: '
            'Post "http://127.0.0.1:8126/v0.6/stats": context deadline exceeded '
            "(occurred: 02 Jan 26 03:09 UTC)"
        ),
    ]

    assert len(fingerprint_and_group(logs, "payments", "us")) == 2


def test_tracer_pattern_does_not_suppress_application_errors() -> None:
    """The pattern matches the tracer's own log prefix, not every mention of the
    vendor, so a service failing to reach Datadog is still investigated."""
    logs = [log("failed to publish metric to Datadog: connection refused")]

    groups = fingerprint_and_group(logs, "payments", "us", ignore_patterns=["Datadog Tracer"])

    assert len(groups) == 1


def test_same_message_from_different_services_does_not_group() -> None:
    payments = fingerprint_and_group([log("connection refused")], "payments", "us")
    billing = fingerprint_and_group([log("connection refused")], "billing", "us")

    assert payments[0].fingerprint != billing[0].fingerprint


def test_group_tracks_first_and_last_seen_across_entries() -> None:
    logs = [
        log("connection refused", timestamp="2026-07-18T12:00:00Z"),
        log("connection refused", timestamp="2026-07-18T09:00:00Z"),
        log("connection refused", timestamp="2026-07-18T15:00:00Z"),
    ]

    groups = fingerprint_and_group(logs, "payments", "us")

    assert groups[0].first_seen.hour == 9
    assert groups[0].last_seen.hour == 15


def test_unparseable_timestamp_does_not_drop_the_log() -> None:
    groups = fingerprint_and_group([log("connection refused", timestamp="not-a-date")], "p", "us")

    assert len(groups) == 1


def test_empty_input_produces_no_groups() -> None:
    assert fingerprint_and_group([], "payments", "us") == []


class TestNormalization:
    def test_uuids_are_replaced(self) -> None:
        msg = normalize_message("user 3f2504e0-4f89-11d3-9a0c-0305e82c3301 not found")

        assert msg == "user <UUID> not found"

    def test_short_numbers_are_preserved(self) -> None:
        """Status codes and small counts carry meaning; only long IDs are noise."""
        assert normalize_message("upstream returned 503") == "upstream returned 503"

    def test_slash_timestamps_are_replaced(self) -> None:
        assert normalize_message("failed at 2026/07/13 09:47:33") == "failed at <TS>"

    def test_whitespace_is_collapsed(self) -> None:
        assert normalize_message("too    many\n\nspaces") == "too many spaces"

    def test_output_is_length_capped(self) -> None:
        assert len(normalize_message("x" * 500)) == 200


class TestStackSignature:
    def test_signature_uses_innermost_frames(self) -> None:
        traceback = (
            'File "/app/src/handler.py", line 10, in handle\n'
            'File "/app/src/db.py", line 22, in query\n'
        )

        assert extract_stack_signature(traceback) == "handler.py:handle|db.py:query"

    def test_messages_without_a_traceback_have_no_signature(self) -> None:
        assert extract_stack_signature("connection refused") == ""

    def test_differing_line_numbers_produce_the_same_signature(self) -> None:
        first = extract_stack_signature('File "/app/db.py", line 22, in query')
        second = extract_stack_signature('File "/app/db.py", line 87, in query')

        assert first == second


class TestErrorFieldExtraction:
    def test_structured_error_object(self) -> None:
        entry = {"attributes": {"attributes": {"error": {"kind": "ValueError", "message": "bad"}}}}

        assert extract_error_fields(entry) == ("ValueError", "bad")

    def test_flat_string_error(self) -> None:
        entry = {"attributes": {"attributes": {"error": "boom", "error.kind": "PanicError"}}}

        assert extract_error_fields(entry) == ("PanicError", "boom")

    def test_error_type_falls_back_when_absent(self) -> None:
        entry = {"attributes": {"attributes": {"error": {"message": "bad"}}}}

        error_type, _ = extract_error_fields(entry)

        assert error_type == "UnknownError"

    def test_message_falls_back_to_top_level_message(self) -> None:
        entry = {"attributes": {"message": "outer message", "attributes": {}}}

        _, message = extract_error_fields(entry)

        assert message == "outer message"

    def test_empty_entry_does_not_raise(self) -> None:
        """An absent error field is treated as a flat string, so the type falls
        back to the log level rather than the "UnknownError" used by the dict
        branch. Documented here because the two branches disagree."""
        error_type, message = extract_error_fields({})

        assert error_type == "error"
        assert message == ""
