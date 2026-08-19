from __future__ import annotations

import logging
import re
from typing import Any

import requests

from ...models import ErrorGroup, Investigation

log = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"

_SEVERITY_PRIORITY = {
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}


class LinearTracker:
    """Issue tracker backed by the Linear GraphQL API."""

    def __init__(self, api_key: str, team_id: str):
        self._api_key = api_key
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        self._state_cache: dict[str, str] = {}  # name_lower -> id
        self._label_cache: dict[str, str] = {}  # name_lower -> id
        self._team_id = self._resolve_team_id(team_id)

    # ------------------------------------------------------------------
    # Protocol: IssueTracker
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._team_id)

    def create(self, error: ErrorGroup, investigation: Investigation) -> str:
        """Create a Linear issue and return its URL."""
        title = f"[{error.service}] {investigation.root_cause[:60]}"
        # The issue is the RCA of record wherever the doc sink is disabled, so
        # it carries every section the doc sink would have written.
        parts = [
            f"**Service:** {error.service} ({error.region})",
            f"**Severity:** {investigation.severity}",
            f"**Occurrences:** {error.occurrence_count}",
            f"**Fingerprint:** `{error.fingerprint}`",
            "",
            "## Root Cause",
            investigation.root_cause or "_Not determined._",
            "",
            "## Impact",
            f"{error.occurrence_count} occurrences since {error.first_seen}",
            "",
            "## Recommendation",
            investigation.recommendation or "_None._",
        ]
        if investigation.fix_description:
            parts += ["", "## Fix Description", investigation.fix_description]
        if investigation.affected_files:
            parts += ["", "## Affected Files"]
            parts += [f"- `{f}`" for f in investigation.affected_files]
        description = "\n".join(parts)
        priority = _SEVERITY_PRIORITY.get(investigation.severity, 3)

        # Resolve label IDs
        label_ids = []
        for name in [error.service, "automated-investigation"]:
            lid = self._get_or_create_label(name)
            if lid:
                label_ids.append(lid)

        variables: dict[str, Any] = {
            "input": {
                "teamId": self._team_id,
                "title": title[:200],
                "description": description[:10000],
                "priority": priority,
            }
        }
        if label_ids:
            variables["input"]["labelIds"] = label_ids

        try:
            data = self._graphql(
                """mutation($input: IssueCreateInput!) {
                    issueCreate(input: $input) {
                        success
                        issue { id identifier url }
                    }
                }""",
                variables,
            )
            result = data.get("issueCreate", {})
            if not result.get("success"):
                log.warning("Linear issueCreate reported failure")
                return ""
            issue = result.get("issue", {})
            url = issue.get("url", "")
            log.info("Created Linear issue %s: %s", issue.get("identifier"), url)
            return url
        except Exception as e:
            log.warning("Failed to create Linear issue: %s", e)
            return ""

    def comment(self, issue_ref: str, body: str) -> None:
        """Add a comment to an existing issue."""
        try:
            identifier = self._extract_issue_identifier(issue_ref)
            if not identifier:
                log.warning("Could not extract identifier from Linear ref: %s", issue_ref)
                return

            issue_id = self._get_issue_id(identifier)
            if not issue_id:
                log.warning("Could not find Linear issue for identifier: %s", identifier)
                return

            data = self._graphql(
                """mutation($input: CommentCreateInput!) {
                    commentCreate(input: $input) {
                        success
                    }
                }""",
                {"input": {"issueId": issue_id, "body": body[:10000]}},
            )
            if data.get("commentCreate", {}).get("success"):
                log.info("Commented on Linear issue %s", identifier)
            else:
                log.warning("Linear commentCreate reported failure for %s", identifier)
        except Exception as e:
            log.warning("Failed to comment on Linear issue %s: %s", issue_ref, e)

    def attach_pr(self, issue_ref: str, pr_url: str, pr_title: str) -> None:
        """Attach a PR link and transition to In Review."""
        try:
            identifier = self._extract_issue_identifier(issue_ref)
            if not identifier:
                log.warning("Could not extract identifier from Linear ref: %s", issue_ref)
                return

            issue_id = self._get_issue_id(identifier)
            if not issue_id:
                log.warning("Could not find Linear issue for identifier: %s", identifier)
                return

            self._attach_url(issue_id, pr_url, pr_title)
            self._transition_to(issue_id, "In Review")
        except Exception as e:
            log.warning("Failed to attach PR to Linear issue %s: %s", issue_ref, e)

    def transition(self, issue_ref: str, state: str) -> None:
        """Transition an issue. Supported states: 'in_review', 'done'."""
        try:
            identifier = self._extract_issue_identifier(issue_ref)
            if not identifier:
                return
            issue_id = self._get_issue_id(identifier)
            if not issue_id:
                return

            state_map = {"in_review": "In Review", "done": "Done"}
            target = state_map.get(state)
            if target:
                self._transition_to(issue_id, target)
        except Exception as e:
            log.warning("Failed to transition Linear issue %s: %s", issue_ref, e)

    # ------------------------------------------------------------------
    # Internal helpers (ported from linear_client.py)
    # ------------------------------------------------------------------

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = requests.post(LINEAR_API, json=payload, headers=self._headers, timeout=15)
        if not resp.ok:
            log.error("Linear API HTTP %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            log.error("Linear GraphQL errors: %s", data["errors"])
        return data.get("data", {})

    def _resolve_team_id(self, team_id: str) -> str:
        """Resolve a team key (e.g. MES) to a UUID if needed."""
        if "-" in team_id:
            return team_id
        try:
            data = self._graphql(
                """query($key: String!) {
                    teams(filter: { key: { eq: $key } }) {
                        nodes { id key }
                    }
                }""",
                {"key": team_id},
            )
            nodes = data.get("teams", {}).get("nodes", [])
            if nodes:
                resolved = nodes[0]["id"]
                log.info("Resolved team key %s to UUID %s", team_id, resolved)
                return resolved
        except Exception as e:
            log.warning("Failed to resolve team key %s: %s", team_id, e)
        return team_id

    @staticmethod
    def _extract_issue_identifier(ref: str) -> str | None:
        """Extract issue identifier (e.g. MSG-123) from a Linear URL or identifier string."""
        match = re.search(r"([A-Z]+-\d+)", ref)
        return match.group(1) if match else None

    def _get_issue_id(self, identifier: str) -> str | None:
        """Look up the internal UUID for a Linear issue by its identifier."""
        data = self._graphql(
            """query($id: String!) {
                issue(id: $id) { id identifier }
            }""",
            {"id": identifier},
        )
        issue = data.get("issue")
        return issue["id"] if issue else None

    def _get_state_id(self, name: str) -> str | None:
        """Find a workflow state by name for the configured team. Cached."""
        name_lower = name.lower()
        if name_lower in self._state_cache:
            return self._state_cache[name_lower]

        if not self._state_cache:
            data = self._graphql(
                """query($teamId: ID!) {
                    workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                        nodes { id name type }
                    }
                }""",
                {"teamId": self._team_id},
            )
            for state in data.get("workflowStates", {}).get("nodes", []):
                self._state_cache[state["name"].lower()] = state["id"]

        state_id = self._state_cache.get(name_lower)
        if not state_id:
            log.warning("Could not find '%s' state for team %s", name, self._team_id)
        return state_id

    def _transition_to(self, issue_id: str, state_name: str) -> bool:
        state_id = self._get_state_id(state_name)
        if not state_id:
            return False

        data = self._graphql(
            """mutation($issueId: String!, $input: IssueUpdateInput!) {
                issueUpdate(id: $issueId, input: $input) {
                    success
                }
            }""",
            {"issueId": issue_id, "input": {"stateId": state_id}},
        )
        success = data.get("issueUpdate", {}).get("success", False)
        if success:
            log.info("Transitioned Linear issue %s to %s", issue_id, state_name)
        return success

    def _attach_url(self, issue_id: str, url: str, title: str) -> bool:
        data = self._graphql(
            """mutation($issueId: String!, $url: String!, $title: String!) {
                attachmentLinkURL(issueId: $issueId, url: $url, title: $title) {
                    success
                }
            }""",
            {"issueId": issue_id, "url": url, "title": title},
        )
        success = data.get("attachmentLinkURL", {}).get("success", False)
        if success:
            log.info("Attached URL to Linear issue %s", issue_id)
        return success

    def _get_or_create_label(self, name: str) -> str | None:
        """Get or create a team label by name. Returns the label ID."""
        name_lower = name.lower()
        if name_lower in self._label_cache:
            return self._label_cache[name_lower]

        # Try to find existing label
        try:
            data = self._graphql(
                """query($teamId: ID!) {
                    issueLabels(filter: { team: { id: { eq: $teamId } } }) {
                        nodes { id name }
                    }
                }""",
                {"teamId": self._team_id},
            )
            for label in data.get("issueLabels", {}).get("nodes", []):
                self._label_cache[label["name"].lower()] = label["id"]

            if name_lower in self._label_cache:
                return self._label_cache[name_lower]

            # Create the label
            data = self._graphql(
                """mutation($input: IssueLabelCreateInput!) {
                    issueLabelCreate(input: $input) {
                        success
                        issueLabel { id name }
                    }
                }""",
                {"input": {"name": name, "teamId": self._team_id}},
            )
            result = data.get("issueLabelCreate", {})
            if result.get("success"):
                label = result.get("issueLabel", {})
                self._label_cache[label["name"].lower()] = label["id"]
                return label["id"]
        except Exception as e:
            log.warning("Failed to get/create label '%s': %s", name, e)
        return None
