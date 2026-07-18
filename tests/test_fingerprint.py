from src.fingerprint import compute_fingerprint, normalize_message


def test_normalize_message_removes_dynamic_identifiers() -> None:
    message = "request 123456 failed for 10.2.3.4 at 2026-07-18T09:10:11Z"

    assert normalize_message(message) == "request <ID> failed for <IP> at <TS>"


def test_fingerprint_is_stable_for_dynamic_values() -> None:
    first = compute_fingerprint(
        "payments",
        "TimeoutError",
        "request 123456 failed at 2026-07-18T09:10:11Z",
    )
    second = compute_fingerprint(
        "payments",
        "TimeoutError",
        "request 987654 failed at 2026-07-18T09:11:12Z",
    )

    assert first == second

