from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from .models import ErrorGroup

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*Z?")
# Non-ISO timestamps like "2026/07/13 09:47:33"
SLASH_TS_RE = re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}")
NUMERIC_ID_RE = re.compile(r"\b\d{5,}\b")
IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
# Base64-encoded tokens/IDs: requires mixed case + digits/special, or '=' padding
BASE64_TOKEN_RE = re.compile(
    r"(?:"
    r"[A-Za-z0-9+/\-_]{6,}={1,2}"   # anything with '=' padding is definitely base64
    r"|"
    r"(?=[A-Za-z0-9+/\-_]*[A-Z])(?=[A-Za-z0-9+/\-_]*[a-z])(?=[A-Za-z0-9+/\-_]*[0-9+/\-_])"
    r"[A-Za-z0-9+/\-_]{8,}"          # mixed case + digits/special (not a plain word)
    r")"
)
WHITESPACE_RE = re.compile(r"\s+")
# Matches Python traceback frame lines: File "path", line N, in func
FRAME_RE = re.compile(r'File "([^"]+)", line \d+, in (\w+)')


def normalize_message(message: str) -> str:
    """Strip dynamic parts from an error message to produce a stable template."""
    msg = UUID_RE.sub("<UUID>", message)
    msg = ISO_TS_RE.sub("<TS>", msg)
    msg = SLASH_TS_RE.sub("<TS>", msg)
    msg = IP_RE.sub("<IP>", msg)
    msg = BASE64_TOKEN_RE.sub("<TOK>", msg)
    msg = NUMERIC_ID_RE.sub("<ID>", msg)
    msg = WHITESPACE_RE.sub(" ", msg).strip()
    return msg[:200]


def extract_stack_signature(message: str) -> str:
    """Extract top 3 frame file+function from a Python traceback."""
    frames = FRAME_RE.findall(message)
    if not frames:
        return ""
    top_frames = frames[-3:]
    parts = []
    for filepath, func in top_frames:
        filename = filepath.rsplit("/", 1)[-1]
        parts.append(f"{filename}:{func}")
    return "|".join(parts)


def compute_fingerprint(service: str, error_type: str, message: str) -> str:
    normalized = normalize_message(message)
    stack_sig = extract_stack_signature(message)
    raw = f"{service}|{error_type}|{normalized}|{stack_sig}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def extract_error_fields(log_entry: dict[str, Any]) -> tuple[str, str]:
    """Extract error_type and message from a DD log entry."""
    attrs = log_entry.get("attributes", {})
    content = attrs.get("attributes", {})

    error_field = content.get("error", "")
    if isinstance(error_field, str):
        # Flat string error (e.g. Go services)
        error_type = content.get("error.kind", "") or content.get("exc_type", "") or content.get("level", "error")
        message = error_field or content.get("message", "") or attrs.get("message", "") or ""
    elif isinstance(error_field, dict):
        error_type = (
            error_field.get("kind", "")
            or error_field.get("type", "")
            or content.get("error.kind", "")
            or content.get("exc_type", "")
            or "UnknownError"
        )
        message = (
            error_field.get("message", "")
            or error_field.get("stack", "")
            or content.get("message", "")
            or attrs.get("message", "")
            or ""
        )
    else:
        error_type = content.get("error.kind", "") or content.get("exc_type", "") or "UnknownError"
        message = content.get("error.message", "") or content.get("message", "") or attrs.get("message", "") or ""

    if not error_type:
        error_type = "UnknownError"

    return error_type, message


def build_dd_link(region: str, query: str, from_ts: str, to_ts: str) -> str:
    tld = "eu" if region == "eu" else "com"
    base = f"https://app.datadoghq.{tld}/logs"
    return f"{base}?query={query}&from_ts={from_ts}&to_ts={to_ts}"


def fingerprint_and_group(
    logs: list[dict[str, Any]],
    service: str,
    region: str,
    ignore_patterns: list[str] | None = None,
) -> list[ErrorGroup]:
    """Fingerprint raw DD logs and group by fingerprint."""
    ignore_patterns = ignore_patterns or []
    groups: dict[str, ErrorGroup] = {}

    for log_entry in logs:
        error_type, message = extract_error_fields(log_entry)

        if any(pat.lower() in message.lower() for pat in ignore_patterns):
            continue

        fp = compute_fingerprint(service, error_type, message)

        timestamp_str = log_entry.get("attributes", {}).get("timestamp", "")
        try:
            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.now(timezone.utc)

        if fp in groups:
            g = groups[fp]
            g.occurrence_count += 1
            if ts < g.first_seen:
                g.first_seen = ts
            if ts > g.last_seen:
                g.last_seen = ts
        else:
            groups[fp] = ErrorGroup(
                fingerprint=fp,
                service=service,
                error_type=error_type,
                message_template=normalize_message(message),
                region=region,
                first_seen=ts,
                last_seen=ts,
                occurrence_count=1,
                sample_log=log_entry,
            )

    return sorted(groups.values(), key=lambda g: g.occurrence_count, reverse=True)
