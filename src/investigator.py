from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .config import Config
from .models import ErrorGroup, Investigation, ServiceConfig

log = logging.getLogger(__name__)


def build_investigation_prompt(
    error: ErrorGroup,
    service_config: ServiceConfig,
    config: Config,
    recent_investigations: list[dict] | None = None,
) -> str:
    sample = json.dumps(error.sample_log, indent=2, default=str)[:3000]

    code_locations = []
    for cp in service_config.code_paths:
        repo_path = config.repos.get(cp.repo, "")
        if repo_path:
            code_locations.append(f"  - {repo_path}/{cp.path}")

    code_locations_str = "\n".join(code_locations) if code_locations else "  (none configured)"

    recent_section = ""
    if recent_investigations:
        lines = []
        for inv in recent_investigations:
            parts = [f"- Root cause: {inv['root_cause'][:150]}"]
            if inv.get("linear_issue_url"):
                parts.append(f"  Linear: {inv['linear_issue_url']}")
            if inv.get("pr_url"):
                parts.append(f"  PR: {inv['pr_url']}")
            parts.append(f"  Error message: {inv.get('message_template', 'N/A')}")
            lines.append("\n".join(parts))
        recent_section = f"""
## Recent Investigations for {error.service}
The following errors in this service have ALREADY been investigated. If the error you are investigating shares the same root cause as one below, it is a duplicate: set is_duplicate=true, put that investigation's issue URL in duplicate_of_url, and set is_fixable=false.

{chr(10).join(lines)}
"""

    return f"""You are investigating a production error.

## READ-ONLY INVESTIGATION
This is an investigation only. You MUST NOT modify, create, or delete any files in the code locations below, and you MUST NOT run git commands that change state (commit, checkout, branch, stash, push, apply, restore). The directories are live developer checkouts. If a fix is warranted, describe it in your JSON output — a separate pipeline applies fixes in an isolated git worktree.
{recent_section}
## Error Details
- Service: {error.service}
- Region: {error.region}
- Error type: {error.error_type}
- Message: {error.message_template}
- Occurrences: {error.occurrence_count}
- First seen: {error.first_seen.isoformat()}
- Last seen: {error.last_seen.isoformat()}

## Sample Log Entry
```json
{sample}
```

## Relevant Code Locations
{code_locations_str}

## Your Tasks
1. Read the relevant source code to understand what caused this error
2. Determine the root cause — trace the code path that produces this error
3. Assess whether this is actionable (needs a fix) or expected/transient
4. If actionable, assess whether a code fix under 250 lines could resolve it
5. Identify the specific file(s) and function(s) involved
6. Explicitly reason through whether an automated fix should be attempted. Consider:
   - Is the root cause in code we own and can modify?
   - Is the fix well-scoped and unlikely to cause regressions?
   - Could it be done safely in under 250 lines?
   - Or is the error transient, external, config-related, or too risky for an automated fix?
   Include this reasoning in your output.
7. Decide whether this error is already tracked. It is a duplicate if an existing
   issue — one listed above, or one you find while investigating — covers the same
   root cause, whether or not that issue is already fixed or in review. If so, set
   is_duplicate=true and put the canonical issue's URL in duplicate_of_url; a
   recurrence note is added to that issue instead of a new one being filed. Only
   claim a duplicate when you have the URL of the issue it duplicates.

   is_duplicate is independent of is_actionable: a duplicate of a real, unfixed bug
   is still actionable. Set is_actionable=false only when the error itself needs no
   action (expected, transient, or outside our control).

## Required Output
After completing your investigation, output ONLY a JSON object (no markdown fences):
{{
  "root_cause": "Clear explanation of why this error occurs",
  "is_actionable": true/false,
  "is_fixable": true/false,
  "is_duplicate": true/false,
  "duplicate_of_url": "<URL of the existing issue, or empty string>",
  "estimated_fix_lines": <number or null>,
  "severity": "critical|high|medium|low",
  "affected_files": ["path/to/file.py"],
  "fix_description": "What the fix would involve (if fixable)",
  "recommendation": "What should be done about this error"
}}"""


def build_add_dir_args(service_config: ServiceConfig, config: Config) -> list[str]:
    """Build --add-dir arguments for service-specific code paths."""
    args = []
    for cp in service_config.code_paths:
        repo_path = config.repos.get(cp.repo)
        if repo_path:
            full_path = f"{repo_path}/{cp.path}"
            args.extend(["--add-dir", full_path])
    return args


def budget_args(max_budget: float | None) -> list[str]:
    """Build the --max-budget-usd flag, or nothing when no cap is configured.

    The flag caps API-key billing only; under a Claude subscription it has no
    effect, so it is omitted rather than passed as a misleading no-op.
    """
    if max_budget is None:
        return []
    return ["--max-budget-usd", f"{max_budget:.2f}"]


# The CLI reports transport failures as ordinary result text, e.g.
# "API Error: Connection closed mid-response. The response above may be incomplete."
_API_ERROR_RE = re.compile(r"^(API Error|Execution error|Error:)", re.I)

# One retry: the dominant failure is a dropped connection, which is transient.
MAX_ATTEMPTS = 2


def parse_claude_output(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse Claude CLI JSON output envelope. Returns (result_data, usage_data)."""
    envelope = json.loads(stdout)

    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    if "usage" in envelope:
        u = envelope["usage"]
        usage["input_tokens"] = u.get("input_tokens", 0)
        usage["output_tokens"] = u.get("output_tokens", 0)
    if "total_cost_usd" in envelope:
        usage["cost_usd"] = envelope["total_cost_usd"]
    elif "cost_usd" in envelope:
        usage["cost_usd"] = envelope["cost_usd"]

    text = envelope.get("result", "")
    if not text and isinstance(envelope.get("content"), list):
        text = "".join(
            b.get("text", "") for b in envelope["content"] if b.get("type") == "text"
        )

    # A call that died mid-stream still returns a well-formed envelope, with the
    # failure described in the result text. Without this it parses as ordinary
    # output, yielding an investigation that merely looks unactionable.
    if envelope.get("is_error") or _API_ERROR_RE.match(text.strip()):
        return {"api_error": text.strip()[:500]}, usage

    result = _extract_json(text)
    return result, usage


# Keys from the requested output schema, used to tell the investigation result
# apart from any other JSON object that happens to appear in the output.
_RESULT_KEYS = (
    "root_cause",
    "is_actionable",
    "is_fixable",
    "is_duplicate",
    "severity",
    "recommendation",
    "affected_files",
    "fix_description",
)


def _extract_json(text: str) -> dict[str, Any]:
    """Extract the investigation result object from Claude's output.

    The object is rarely the whole output: Claude tends to explain its finding
    first, and that prose routinely contains braces of its own. Go panic frames
    are the common case — ``OnWrite(0x0?, {0x0, 0x0, 0x0})`` — and a brace-heavy
    stack is exactly what an investigation quotes back. Slicing from the first
    brace to the last therefore swallows the prose and parses nothing, which
    discarded otherwise complete investigations at roughly 17% on Go services
    against 1% elsewhere.

    So decode every brace-led candidate and keep the best: the one carrying the
    most keys of the requested schema, breaking ties on length so an outer
    object beats one nested inside it. This subsumes code-fence handling, since
    a fenced object is found by its brace like any other.
    """
    text = (text or "").strip()
    decoder = json.JSONDecoder()

    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            candidate, end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        score = (sum(k in candidate for k in _RESULT_KEYS), end - i)
        if best is None or score > best[0]:
            best = (score, candidate)

    if best is not None:
        return best[1]

    log.warning("Failed to parse investigation JSON, returning raw text")
    return {"root_cause": text[:500], "parse_error": True}


def investigate(
    error: ErrorGroup,
    service_config: ServiceConfig,
    config: Config,
    recent_investigations: list[dict] | None = None,
) -> Investigation:
    """Run Claude CLI to investigate an error. Returns Investigation with results."""
    investigation = Investigation(
        id=str(uuid4()),
        fingerprint=error.fingerprint,
        started_at=datetime.now(timezone.utc),
    )

    prompt = build_investigation_prompt(error, service_config, config, recent_investigations)
    add_dir_args = build_add_dir_args(service_config, config)

    cmd = [
        config.claude_bin,
        "-p",
        "--model", config.claude_model,
        "--effort", config.claude_effort,
        *budget_args(config.claude_max_budget),
        "--output-format", "json",
        # Investigations are strictly read-only: any code fix must go through
        # the fixer pipeline, which works in an isolated git worktree.
        "--disallowedTools", "Edit,Write,NotebookEdit",
        *add_dir_args,
    ]

    log.info(
        "Starting Claude investigation for %s/%s (fingerprint: %s)",
        error.service, error.error_type, error.fingerprint,
    )

    data: dict[str, Any] = {}
    failure_reason = ""
    failure_detail = ""

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=900,
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            if not stdout:
                failure_reason = "no_output"
                failure_detail = f"Claude produced no output (exit={result.returncode}). stderr: {stderr[:300]}"
            else:
                data, usage = parse_claude_output(stdout)
                # Charged whether or not the attempt produced anything usable.
                investigation.claude_cost_usd += usage.get("cost_usd", 0.0)
                investigation.claude_input_tokens += usage.get("input_tokens", 0)
                investigation.claude_output_tokens += usage.get("output_tokens", 0)

                if data.get("api_error"):
                    failure_reason = "api_error"
                    failure_detail = data["api_error"]
                elif data.get("parse_error"):
                    failure_reason = "unparseable_output"
                    failure_detail = data.get("root_cause", "")
                else:
                    failure_reason = ""
                    failure_detail = ""
                    break

            if attempt < MAX_ATTEMPTS:
                log.warning(
                    "Investigation attempt %d/%d for %s failed (%s); retrying",
                    attempt, MAX_ATTEMPTS, error.fingerprint, failure_reason,
                )

        # Recorded as failed rather than complete so the pipeline re-queues the
        # error instead of closing it with a result nobody produced.
        if failure_reason:
            investigation.status = "failed"
            investigation.failure_reason = failure_reason
            investigation.root_cause = failure_detail[:500]
            investigation.completed_at = datetime.now(timezone.utc)
            log.error(
                "Investigation failed for %s after %d attempts (%s), cost=$%.4f",
                error.fingerprint, MAX_ATTEMPTS, failure_reason,
                investigation.claude_cost_usd,
            )
            return investigation

        investigation.root_cause = data.get("root_cause", "")
        investigation.is_actionable = bool(data.get("is_actionable", False))
        investigation.is_fixable = bool(data.get("is_fixable", False))
        investigation.duplicate_of_url = (data.get("duplicate_of_url") or "").strip()
        # A duplicate claim without a target issue is unusable — we would have
        # nowhere to record the recurrence — so it falls back to filing normally.
        investigation.is_duplicate = bool(
            data.get("is_duplicate", False) and investigation.duplicate_of_url
        )
        if data.get("is_duplicate") and not investigation.duplicate_of_url:
            log.warning(
                "Investigation for %s claimed duplicate but gave no issue URL; filing normally",
                error.fingerprint,
            )
        investigation.estimated_fix_lines = data.get("estimated_fix_lines")
        investigation.severity = data.get("severity", "medium")
        investigation.affected_files = data.get("affected_files", [])
        investigation.fix_description = data.get("fix_description", "")
        investigation.recommendation = data.get("recommendation", "")
        # notion_page_url / linear_issue_url are set by the pipeline, not the model.
        investigation.status = "complete"
        investigation.completed_at = datetime.now(timezone.utc)

        log.info(
            "Investigation complete: actionable=%s fixable=%s duplicate=%s severity=%s cost=$%.4f",
            investigation.is_actionable,
            investigation.is_fixable,
            investigation.duplicate_of_url if investigation.is_duplicate else False,
            investigation.severity,
            investigation.claude_cost_usd,
        )

    except subprocess.TimeoutExpired:
        log.error("Claude investigation timed out for %s", error.fingerprint)
        investigation.status = "failed"
        investigation.failure_reason = "timeout"
        investigation.root_cause = "Investigation timed out after 900s"
        investigation.completed_at = datetime.now(timezone.utc)

    except Exception as e:
        log.error("Claude investigation failed for %s: %s", error.fingerprint, e)
        investigation.status = "failed"
        investigation.failure_reason = "error"
        investigation.root_cause = f"Investigation error: {e}"
        investigation.completed_at = datetime.now(timezone.utc)

    return investigation
