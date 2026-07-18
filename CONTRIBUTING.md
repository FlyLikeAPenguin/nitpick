# Contributing to Nitpick

Thanks for helping make incident response less repetitive. Small documentation, test, and provider improvements are welcome.

## Before you start

- Search existing issues before opening a new one.
- For a larger change, open an issue first so the design and scope are clear.
- Never include credentials, production log payloads, or private source code in an issue or pull request.

## Local development

Nitpick supports Python 3.12 and newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

The unit tests do not call Datadog, Claude, GitHub, Linear, Notion, or Slack. Keep external calls behind provider boundaries and use deterministic fixtures or mocks in tests.

## Pull requests

Please keep pull requests focused and include:

- the problem and user impact
- a short explanation of the implementation
- tests added or updated
- documentation updates for changed behavior
- any security, privacy, or cost implications

Generated fixes must remain reviewable pull requests. Do not add behavior that writes to a live checkout or bypasses the existing safety controls.

## Good first contributions

- Add provider contract tests.
- Improve the dashboard with accessible labels and keyboard support.
- Add sanitized log fixtures for additional error formats.
- Improve setup documentation for Linux and macOS scheduling.
