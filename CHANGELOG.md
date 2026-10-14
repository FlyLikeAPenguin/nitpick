# Changelog

All notable changes to Nitpick are documented here.

## [Unreleased]

- Record recurrences of an already-tracked error on the existing issue instead of filing a new one.
- Link a recurrence to its existing issue even when the investigator calls it non-actionable.
- Normalise hex addresses when fingerprinting, so recurring Go panics group together.
- Gate documenting and issue-filing as independent stages (`docs_enabled`, `issues_enabled`).
- Retry investigations that return no usable result, and record them as failed with a `failure_reason` instead of complete.
- Link filed issues back to their Datadog logs.
- Add `scripts/install.sh` to set up the macOS LaunchAgents for the current user.
- Add safe-by-default automatic-fix controls.
- Add tests, CI, contributor guidance, and security reporting guidance.
- Improve the README with setup, safety, limitations, and development documentation.

## [0.1.0] - 2026-07-18

- Initial public release of the Datadog-to-Claude incident investigation pipeline.
- Add local dashboard, SQLite deduplication, provider adapters, RCA sinks, issue tracking, notifications, and optional fix PRs.
