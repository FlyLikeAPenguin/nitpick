from __future__ import annotations

import logging
import re

import requests

from ...models import ErrorGroup, Investigation

log = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"


class NotionDocSink:
    """Create Notion pages for RCA documents and add comments."""

    def __init__(self, api_key: str, database_id: str):
        self._api_key = api_key
        self._database_id = database_id
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._database_id)

    def create(self, error: ErrorGroup, investigation: Investigation) -> str:
        """Create a Notion page with RCA details. Returns the page URL."""
        title = f"[{error.service}] {error.error_type}"

        properties = {
            "Title": {"title": [{"text": {"content": title[:200]}}]},
            "Service": {"rich_text": [{"text": {"content": error.service}}]},
            "Region": {"rich_text": [{"text": {"content": error.region}}]},
            "Severity": {"rich_text": [{"text": {"content": investigation.severity}}]},
            "Fingerprint": {"rich_text": [{"text": {"content": error.fingerprint}}]},
        }

        # Build body blocks
        children: list[dict] = []

        def _paragraph(text: str) -> dict:
            # Notion limits rich_text content to 2000 chars per block
            return {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
                },
            }

        def _heading(text: str) -> dict:
            return {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                },
            }

        children.append(_heading("Root Cause"))
        children.append(_paragraph(investigation.root_cause or "Not determined."))

        children.append(_heading("Recommendation"))
        children.append(_paragraph(investigation.recommendation or "None."))

        children.append(_heading("Fix Description"))
        children.append(_paragraph(investigation.fix_description or "None."))

        if investigation.affected_files:
            children.append(_heading("Affected Files"))
            for fpath in investigation.affected_files:
                children.append(_paragraph(fpath))

        try:
            resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=self._headers,
                json={
                    "parent": {"database_id": self._database_id},
                    "properties": properties,
                    "children": children,
                },
                timeout=15,
            )
            if not resp.ok:
                log.warning(
                    "Notion page creation failed (%d): %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return ""
            page = resp.json()
            url = page.get("url", "")
            log.info("Created Notion RCA page: %s", url)
            return url
        except Exception as e:
            log.warning("Failed to create Notion page: %s", e)
            return ""

    def comment(self, doc_ref: str, message: str) -> None:
        """Add a comment to a Notion page identified by URL."""
        page_id = _extract_notion_page_id(doc_ref)
        if not page_id:
            log.warning("Could not extract page ID from Notion URL: %s", doc_ref)
            return

        try:
            resp = requests.post(
                "https://api.notion.com/v1/comments",
                headers=self._headers,
                json={
                    "parent": {"page_id": page_id},
                    "rich_text": [{"type": "text", "text": {"content": message}}],
                },
                timeout=10,
            )
            if resp.ok:
                log.info("Added Notion comment to %s", page_id)
            else:
                log.warning(
                    "Notion comment failed (%d): %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as e:
            log.warning("Failed to add Notion comment: %s", e)


def _extract_notion_page_id(url: str) -> str | None:
    """Extract the page ID from a Notion URL and format as UUID.

    URLs look like: https://www.notion.so/workspace/Title-<32hex>
    or: https://www.notion.so/<32hex>
    """
    match = re.search(r"([0-9a-f]{32})(?:\?|$)", url)
    if match:
        hex_id = match.group(1)
        return f"{hex_id[:8]}-{hex_id[8:12]}-{hex_id[12:16]}-{hex_id[16:20]}-{hex_id[20:]}"
    # Already UUID-formatted
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url
    )
    return match.group(1) if match else None
