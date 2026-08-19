from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class CodePath:
    repo: str
    path: str


@dataclass
class ServiceConfig:
    name: str
    datadog_query: str
    regions: list[str]
    code_paths: list[CodePath]


@dataclass
class ErrorGroup:
    fingerprint: str
    service: str
    error_type: str
    message_template: str
    region: str
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int = 1
    status: str = "new"
    sample_log: dict[str, Any] = field(default_factory=dict)
    dd_query_link: str = ""

    def sample_log_json(self) -> str:
        return json.dumps(self.sample_log, default=str)


@dataclass
class Investigation:
    id: str
    fingerprint: str
    started_at: datetime
    completed_at: datetime | None = None
    root_cause: str = ""
    is_actionable: bool = False
    is_fixable: bool = False
    # A duplicate is still actionable — it just belongs on an existing issue
    # rather than a new one. duplicate_of_url is the issue it belongs to.
    is_duplicate: bool = False
    duplicate_of_url: str = ""
    estimated_fix_lines: int | None = None
    severity: str = "medium"
    affected_files: list[str] = field(default_factory=list)
    fix_description: str = ""
    recommendation: str = ""
    notion_page_url: str = ""
    linear_issue_url: str = ""
    linear_issue_id: str = ""
    pr_url: str = ""
    pr_status: str = ""
    branch_name: str = ""
    fix_error: str = ""
    claude_cost_usd: float = 0.0
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    status: str = "pending"


@dataclass
class RunStats:
    errors_fetched: int = 0
    new_errors: int = 0
    investigated: int = 0
    issues_created: int = 0
    duplicates_linked: int = 0
    prs_created: int = 0
    total_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "errors_fetched": self.errors_fetched,
            "new_errors": self.new_errors,
            "investigated": self.investigated,
            "issues_created": self.issues_created,
            "duplicates_linked": self.duplicates_linked,
            "prs_created": self.prs_created,
            "total_cost_usd": self.total_cost_usd,
        }
