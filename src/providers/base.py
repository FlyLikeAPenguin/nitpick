from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models import ErrorGroup, Investigation


@runtime_checkable
class ErrorSource(Protocol):
    @property
    def is_configured(self) -> bool: ...
    def fetch(self, query: str, lookback_seconds: int, limit: int) -> list[dict[str, Any]]: ...


@runtime_checkable
class DocSink(Protocol):
    @property
    def is_configured(self) -> bool: ...
    def create(self, error: ErrorGroup, investigation: Investigation) -> str: ...
    def comment(self, doc_ref: str, message: str) -> None: ...


@runtime_checkable
class IssueTracker(Protocol):
    @property
    def is_configured(self) -> bool: ...
    def create(self, error: ErrorGroup, investigation: Investigation) -> str: ...
    def attach_pr(self, issue_ref: str, pr_url: str, pr_title: str) -> None: ...
    def transition(self, issue_ref: str, state: str) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    @property
    def is_configured(self) -> bool: ...
    def notify(
        self,
        error: ErrorGroup,
        investigation: Investigation,
        config: Any,
        subscribers: list[str] | None = None,
    ) -> None: ...
    def notify_daily_limit(self, count: int, limit: int) -> None: ...
