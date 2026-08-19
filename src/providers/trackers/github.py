from __future__ import annotations

import logging
import shutil
import subprocess

from ...models import ErrorGroup, Investigation

log = logging.getLogger(__name__)


class GitHubTracker:
    """Issue tracker backed by the ``gh`` CLI -- zero-SaaS default."""

    def __init__(self, default_repo: str = ""):
        self._repo = default_repo or self._detect_repo()

    @staticmethod
    def _detect_repo() -> str:
        """Try to derive owner/repo from the git remote."""
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @property
    def is_configured(self) -> bool:
        return shutil.which("gh") is not None

    def create(self, error: ErrorGroup, investigation: Investigation) -> str:
        """Create a GitHub issue and return its URL."""
        title = f"[{error.service}] {investigation.root_cause[:60]}"
        body_parts = [
            f"**Service:** {error.service} ({error.region})",
            f"**Severity:** {investigation.severity}",
            f"**Occurrences:** {error.occurrence_count}",
            f"**Fingerprint:** `{error.fingerprint}`",
        ]
        if error.dd_query_link:
            body_parts.append(f"**Logs:** [Datadog]({error.dd_query_link})")
        body_parts += [
            "",
            "## Root Cause",
            investigation.root_cause or "_Not determined._",
            "",
            "## Recommendation",
            investigation.recommendation or "_None._",
        ]
        if investigation.fix_description:
            body_parts.append("")
            body_parts.append("## Fix Description")
            body_parts.append(investigation.fix_description)
        if investigation.affected_files:
            body_parts.append("")
            body_parts.append("## Affected Files")
            for f in investigation.affected_files:
                body_parts.append(f"- `{f}`")

        body = "\n".join(body_parts)

        cmd = ["gh", "issue", "create", "--title", title, "--body", body]
        if self._repo:
            cmd.extend(["--repo", self._repo])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                log.warning("gh issue create failed: %s", result.stderr.strip()[:200])
                return ""
            url = result.stdout.strip()
            log.info("Created GitHub issue: %s", url)
            return url
        except Exception as e:
            log.warning("Failed to create GitHub issue: %s", e)
            return ""

    def comment(self, issue_ref: str, body: str) -> None:
        """Add a comment to an existing issue."""
        cmd = ["gh", "issue", "comment", issue_ref, "--body", body]
        if self._repo:
            cmd.extend(["--repo", self._repo])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                log.warning("gh issue comment failed: %s", result.stderr.strip()[:200])
            else:
                log.info("Commented on GitHub issue %s", issue_ref)
        except Exception as e:
            log.warning("Failed to comment on GitHub issue %s: %s", issue_ref, e)

    def attach_pr(self, issue_ref: str, pr_url: str, pr_title: str) -> None:
        """Comment on the issue linking the PR."""
        self.comment(issue_ref, f"PR opened: [{pr_title}]({pr_url})")

    def transition(self, issue_ref: str, state: str) -> None:
        """Close the issue if *state* is 'done', otherwise no-op."""
        if state != "done":
            return
        cmd = ["gh", "issue", "close", issue_ref]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                log.warning("gh issue close failed: %s", result.stderr.strip()[:200])
            else:
                log.info("Closed GitHub issue %s", issue_ref)
        except Exception as e:
            log.warning("Failed to close GitHub issue %s: %s", issue_ref, e)
