from __future__ import annotations

import json
import logging
import subprocess
import tempfile
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
The following errors in this service have ALREADY been investigated. If the error you are investigating shares the same root cause as one below, DO NOT recommend a duplicate fix. Instead, reference the existing issue URL in your output and set is_fixable=false.

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

## Required Output
After completing your investigation, output ONLY a JSON object (no markdown fences):
{{
  "root_cause": "Clear explanation of why this error occurs",
  "is_actionable": true/false,
  "is_fixable": true/false,
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

    result = _extract_json(text)
    return result, usage


def _extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from text, handling code fences."""
    text = text.strip()

    import re
    fence_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", text, re.I)
    if fence_match:
        text = fence_match.group(1).strip()

    if not text.startswith("{"):
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            text = text[brace_start : brace_end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
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
        "--model", "opus",
        "--max-budget-usd", str(config.claude_max_budget),
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

    try:
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
            raise RuntimeError(f"Claude produced no output (exit={result.returncode}). stderr: {stderr[:500]}")

        data, usage = parse_claude_output(stdout)

        investigation.root_cause = data.get("root_cause", "")
        investigation.is_actionable = bool(data.get("is_actionable", False))
        investigation.is_fixable = bool(data.get("is_fixable", False))
        investigation.estimated_fix_lines = data.get("estimated_fix_lines")
        investigation.severity = data.get("severity", "medium")
        investigation.affected_files = data.get("affected_files", [])
        investigation.fix_description = data.get("fix_description", "")
        investigation.recommendation = data.get("recommendation", "")
        investigation.notion_page_url = data.get("notion_page_url", "")
        investigation.linear_issue_url = data.get("linear_issue_url", "")
        investigation.claude_cost_usd = usage.get("cost_usd", 0.0)
        investigation.claude_input_tokens = usage.get("input_tokens", 0)
        investigation.claude_output_tokens = usage.get("output_tokens", 0)
        investigation.status = "complete"
        investigation.completed_at = datetime.now(timezone.utc)

        log.info(
            "Investigation complete: actionable=%s fixable=%s severity=%s cost=$%.4f",
            investigation.is_actionable,
            investigation.is_fixable,
            investigation.severity,
            investigation.claude_cost_usd,
        )

    except subprocess.TimeoutExpired:
        log.error("Claude investigation timed out for %s", error.fingerprint)
        investigation.status = "failed"
        investigation.root_cause = "Investigation timed out after 900s"
        investigation.completed_at = datetime.now(timezone.utc)

    except Exception as e:
        log.error("Claude investigation failed for %s: %s", error.fingerprint, e)
        investigation.status = "failed"
        investigation.root_cause = f"Investigation error: {e}"
        investigation.completed_at = datetime.now(timezone.utc)

    return investigation
