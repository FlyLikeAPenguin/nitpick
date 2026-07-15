from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ...models import ErrorGroup, Investigation

log = logging.getLogger(__name__)

# Default output alongside the SQLite DB
_DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "rca"


class MarkdownDocSink:
    """Write RCA documents as local Markdown files -- zero-SaaS default."""

    def __init__(self, output_dir: Path | None = None):
        self._output_dir = output_dir or _DEFAULT_OUTPUT_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_configured(self) -> bool:
        return True

    def create(self, error: ErrorGroup, investigation: Investigation) -> str:
        """Write an RCA markdown file and return its path."""
        path = self._output_dir / f"{error.fingerprint}.md"
        try:
            lines = [
                f"# {error.error_type}",
                "",
                f"**Service:** {error.service}",
                f"**Region:** {error.region}",
                f"**Severity:** {investigation.severity}",
                f"**Fingerprint:** `{error.fingerprint}`",
                f"**Occurrences:** {error.occurrence_count}",
                f"**First seen:** {error.first_seen}",
                f"**Last seen:** {error.last_seen}",
                "",
                "## Root Cause",
                "",
                investigation.root_cause or "_Not determined._",
                "",
                "## Recommendation",
                "",
                investigation.recommendation or "_None._",
                "",
                "## Fix Description",
                "",
                investigation.fix_description or "_None._",
                "",
                "## Affected Files",
                "",
            ]
            if investigation.affected_files:
                for f in investigation.affected_files:
                    lines.append(f"- `{f}`")
            else:
                lines.append("_None identified._")
            lines.append("")

            path.write_text("\n".join(lines), encoding="utf-8")
            log.info("Wrote RCA markdown to %s", path)
            return str(path)
        except Exception as e:
            log.warning("Failed to write RCA markdown for %s: %s", error.fingerprint, e)
            return ""

    def comment(self, doc_ref: str, message: str) -> None:
        """Append a timestamped comment to the markdown file."""
        try:
            path = Path(doc_ref)
            if not path.exists():
                log.warning("RCA file not found for comment: %s", doc_ref)
                return
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n---\n**[{ts}]** {message}\n")
            log.info("Appended comment to %s", doc_ref)
        except Exception as e:
            log.warning("Failed to append comment to %s: %s", doc_ref, e)
