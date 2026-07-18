from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .investigator import parse_claude_output
from .models import ErrorGroup, Investigation, ServiceConfig

log = logging.getLogger(__name__)

WORKTREE_BASE = Path("/tmp/nitpick-worktrees")


def _slugify(error: ErrorGroup) -> str:
    """Build a short kebab-case description from the error for use in branch names."""
    import re
    # Combine error_type + start of message for uniqueness
    parts = []
    if error.error_type:
        parts.append(error.error_type)
    if error.message_template:
        # Take first few words of the message for context
        msg_words = error.message_template.split()[:4]
        parts.append(" ".join(msg_words))
    raw = " ".join(parts) if parts else error.fingerprint[:8]
    # Convert CamelCase to kebab
    slug = re.sub(r"([a-z])([A-Z])", r"\1-\2", raw)
    # Replace non-alphanumeric with hyphens, collapse, strip, lowercase
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", slug).strip("-").lower()
    # Truncate, then append fingerprint suffix for guaranteed uniqueness
    slug = slug[:50].rstrip("-")
    return f"{slug}-{error.fingerprint[:6]}"


def cleanup_stale_worktrees(config: Config) -> None:
    """Remove stale worktrees on startup."""
    for repo_path in config.repos.values():
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=repo_path, capture_output=True, timeout=10,
            )
        except Exception:
            pass

    if WORKTREE_BASE.exists():
        for child in WORKTREE_BASE.iterdir():
            if child.is_dir():
                try:
                    shutil.rmtree(child)
                    log.info("Cleaned up stale worktree: %s", child)
                except Exception as e:
                    log.warning("Failed to clean worktree %s: %s", child, e)


def _determine_default_branch(repo_path: str) -> str:
    """Detect whether the repo uses 'main' or 'master'."""
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo_path, capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        return ref.rsplit("/", 1)[-1]

    for candidate in ["main", "master"]:
        check = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{candidate}"],
            cwd=repo_path, capture_output=True, timeout=10,
        )
        if check.returncode == 0:
            return candidate

    return "main"


def _determine_repo_for_fix(
    investigation: Investigation, service_config: ServiceConfig, config: Config
) -> tuple[str, str]:
    """Determine which repo to fix in and the GitHub org/repo name."""
    for af in investigation.affected_files:
        for cp in service_config.code_paths:
            repo_path = config.repos.get(cp.repo, "")
            if repo_path and af.startswith(cp.path):
                remote = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10,
                )
                gh_repo = ""
                if remote.returncode == 0:
                    url = remote.stdout.strip()
                    # git@github.com:Org/Repo.git or https://github.com/Org/Repo.git
                    if "github.com" in url:
                        gh_repo = url.split("github.com")[-1].strip(":/ ").removesuffix(".git")
                return repo_path, gh_repo

    # Default to first configured repo
    repo_path = next(iter(config.repos.values()), ".")
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_path, capture_output=True, text=True, timeout=10,
    )
    gh_repo = ""
    if remote.returncode == 0:
        url = remote.stdout.strip()
        if "github.com" in url:
            gh_repo = url.split("github.com")[-1].strip(":/ ").removesuffix(".git")
    return repo_path, gh_repo


def attempt_fix(
    error: ErrorGroup,
    investigation: Investigation,
    service_config: ServiceConfig,
    config: Config,
) -> str | None:
    """Attempt to fix an error via git worktree + Claude. Returns PR URL or None."""
    repo_path, gh_repo = _determine_repo_for_fix(investigation, service_config, config)
    if not gh_repo:
        log.error("Cannot determine GitHub repo for fix, skipping")
        investigation.fix_error = "Cannot determine GitHub repo"
        return None

    default_branch = _determine_default_branch(repo_path)
    branch_name = f"nitpick/{_slugify(error)}"
    worktree_path = WORKTREE_BASE / branch_name.replace("/", "-")

    WORKTREE_BASE.mkdir(parents=True, exist_ok=True)

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "origin", default_branch],
            cwd=repo_path, capture_output=True, timeout=60,
        )

        # Create worktree
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), f"origin/{default_branch}"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.error("Failed to create worktree: %s", result.stderr)
            investigation.fix_error = f"Failed to create worktree: {result.stderr.strip()}"
            return None

        log.info("Created worktree at %s on branch %s", worktree_path, branch_name)

        # Build fix prompt
        fix_prompt = _build_fix_prompt(error, investigation, service_config)

        # Grant access only inside the worktree — never the developer's live
        # checkout. Paths in the repo being fixed are remapped into the
        # worktree; code paths in other repos are omitted (the fix prompt
        # already carries the investigation context for them).
        add_dir_args = []
        for cp in service_config.code_paths:
            rp = config.repos.get(cp.repo)
            if rp == repo_path:
                add_dir_args.extend(["--add-dir", str(worktree_path / cp.path)])

        cmd = [
            config.claude_bin,
            "-p",
            "--model", "opus",
            "--max-budget-usd", "3.00",
            "--output-format", "json",
            *add_dir_args,
        ]

        log.info("Running Claude fix attempt...")
        fix_result = subprocess.run(
            cmd,
            input=fix_prompt,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=900,
        )

        if fix_result.stdout.strip():
            fix_data, fix_usage = parse_claude_output(fix_result.stdout)
            investigation.claude_cost_usd += fix_usage.get("cost_usd", 0.0)
            investigation.claude_input_tokens += fix_usage.get("input_tokens", 0)
            investigation.claude_output_tokens += fix_usage.get("output_tokens", 0)

        # Validate diff size
        diff_numstat = subprocess.run(
            ["git", "diff", "--numstat", f"origin/{default_branch}"],
            cwd=str(worktree_path), capture_output=True, text=True, timeout=10,
        )

        total_lines = 0
        for line in diff_numstat.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    total_lines += int(parts[0]) + int(parts[1])
                except ValueError:
                    pass

        if total_lines == 0:
            log.warning("Claude made no changes, abandoning fix")
            investigation.fix_error = "Claude made no changes"
            return None

        if total_lines > 250:
            log.warning("Fix too large (%d lines), abandoning", total_lines)
            investigation.fix_error = f"Fix too large ({total_lines} lines, limit 250)"
            return None

        log.info("Fix diff: %d lines changed", total_lines)

        # Stage and commit
        subprocess.run(["git", "add", "-A"], cwd=str(worktree_path), timeout=10)

        short_desc = investigation.fix_description[:60] if investigation.fix_description else investigation.root_cause[:60]
        commit_msg = (
            f"fix({error.service}): {short_desc}\n\n"
            f"Root cause: {investigation.root_cause[:200]}\n\n"
            f"Error-Watcher automated fix\n\n"
            f"Co-Authored-By: Claude <noreply@anthropic.com>"
        )

        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(worktree_path), capture_output=True, timeout=30,
        )

        # Push
        push_result = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=str(worktree_path), capture_output=True, text=True, timeout=60,
        )
        if push_result.returncode != 0:
            log.error("Failed to push: %s", push_result.stderr)
            investigation.fix_error = f"Failed to push: {push_result.stderr.strip()[:200]}"
            return None

        # Create PR
        pr_body = _build_pr_body(error, investigation)
        pr_result = subprocess.run(
            [
                "gh", "pr", "create",
                "--repo", gh_repo,
                "--head", branch_name,
                "--base", default_branch,
                "--title", f"fix({error.service}): {short_desc}",
                "--body", pr_body,
            ],
            cwd=str(worktree_path), capture_output=True, text=True, timeout=30,
        )

        if pr_result.returncode != 0:
            log.error("Failed to create PR: %s", pr_result.stderr)
            investigation.fix_error = f"Failed to create PR: {pr_result.stderr.strip()[:200]}"
            return None

        pr_url = pr_result.stdout.strip()
        log.info("Created PR: %s", pr_url)

        investigation.pr_url = pr_url
        investigation.pr_status = "open"
        investigation.branch_name = branch_name

        return pr_url

    except subprocess.TimeoutExpired:
        log.error("Fix attempt timed out")
        investigation.fix_error = "Fix attempt timed out (900s)"
        return None
    except Exception as e:
        log.error("Fix attempt failed: %s", e)
        investigation.fix_error = f"Fix attempt failed: {e}"
        return None
    finally:
        # Cleanup worktree
        try:
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=repo_path, capture_output=True, timeout=10,
            )
        except Exception:
            pass
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)


def _build_fix_prompt(
    error: ErrorGroup,
    investigation: Investigation,
    service_config: ServiceConfig,
) -> str:
    affected = "\n".join(f"  - {f}" for f in investigation.affected_files) if investigation.affected_files else "  (see investigation)"

    return f"""You are fixing a production error. The error has been investigated and a fix is feasible.

## Error
- Service: {error.service}
- Error type: {error.error_type}
- Message: {error.message_template}

## Investigation Results
- Root cause: {investigation.root_cause}
- Fix description: {investigation.fix_description}
- Affected files:
{affected}

## Rules
- Work ONLY inside the current directory (an isolated git worktree). Affected file paths are relative to it. Never write outside it, even if a path elsewhere is accessible
- Make the MINIMUM change needed to fix the error
- Do not refactor unrelated code
- Add or update tests if the fix is testable and test files exist nearby
- Keep total diff under 250 lines
- Do not modify CI/CD configuration or deployment files
- Stage all your changes with git add when done

After making the fix, output a brief JSON summary:
{{"files_changed": ["list of files"], "description": "what you changed"}}"""


def _build_pr_body(error: ErrorGroup, investigation: Investigation) -> str:
    sections = [
        "## Summary",
        f"Automated fix for `{error.error_type}` in **{error.service}** ({error.region}).",
        "",
        "## Root Cause",
        investigation.root_cause,
        "",
        "## Fix",
        investigation.fix_description,
        "",
    ]

    if investigation.linear_issue_url:
        sections.extend(["## Linear Issue", investigation.linear_issue_url, ""])
    if investigation.notion_page_url:
        sections.extend(["## RCA Documentation", investigation.notion_page_url, ""])

    sections.extend([
        "---",
        "Generated by nitpick | Co-Authored-By: Claude <noreply@anthropic.com>",
    ])

    return "\n".join(sections)
