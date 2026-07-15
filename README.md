# Nitpick

Automated error monitoring and investigation powered by Claude. Fetches errors from your log provider, investigates each with Claude CLI, documents findings, files issues, and optionally opens fix PRs — all on autopilot.

## How it works

```
Error Source  →  Fingerprint + Dedup  →  Claude CLI Investigation
(Datadog)         (SQLite)                  │
                                            ├── RCA documentation (Markdown / Notion)
                                            ├── Issue filed (GitHub / Linear)
                                            ├── Fix attempt + PR (if <250 lines)
                                            └── Notification (Console / Slack)
```

Every run, the pipeline:

1. Fetches error-level logs from your configured error source
2. Normalises and fingerprints errors (strips UUIDs, timestamps, IPs, numeric IDs) to group duplicates
3. Deduplicates against a local SQLite cache — only new errors are investigated
4. Sends each new error to Claude CLI which reads the relevant source code, performs root cause analysis, and returns structured JSON
5. Creates an RCA document and files an issue via your configured providers
6. If Claude deems the fix feasible and under 250 lines, creates a git worktree, applies the fix, and opens a PR
7. Sends a notification with links to the RCA, issue, and PR

A local web dashboard at `localhost:8111` shows errors, investigations, runs, costs, and live logs.

## Providers

Nitpick uses a pluggable provider model. You choose an adapter for each role via `config.yml`:

| Role | Default | Alternative | What it does |
|---|---|---|---|
| **Error source** | `datadog` | — | Fetches error logs |
| **Doc sink** | `markdown` | `notion` | Creates RCA documentation |
| **Issue tracker** | `github` | `linear` | Creates and manages issues |
| **Notifier** | `console` | `slack` | Sends notifications |

With the defaults, you need only **Datadog + Claude CLI + `gh`** to get full value — no other SaaS accounts required. RCAs are written as local Markdown files and issues are filed as GitHub Issues.

## Prerequisites

- Python 3.12+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- [`gh` CLI](https://cli.github.com/) authenticated (for PR creation and the default GitHub issue tracker)
- Datadog API + App keys for at least one site

## Quickstart

```bash
git clone <this-repo> && cd nitpick
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env — fill in your Datadog keys at minimum
```

Edit `config.yml` to add your services:

```yaml
providers:
  source:    { type: datadog }
  doc_sink:  { type: markdown }
  tracker:   { type: github }
  notifier:  { type: console }

services:
  my-api:
    datadog_query: "service:my-api status:error"
    regions: [us]
    code_paths:
      - repo: backend
        path: src/my_api

repos:
  backend: /path/to/your/backend/repo
```

Run:

```bash
# Single pipeline run
python -m src.main run

# Dry run (fetch + fingerprint only, no Claude)
NITPICK_DRY_RUN=true python -m src.main run

# Start dashboard
python -m src.main serve
```

## Configuration

### Environment variables

Copy `.env.example` to `.env` and fill in credentials:

| Variable | Required | Description |
|---|---|---|
| `DD_US_API_KEY` / `DD_US_APP_KEY` | At least one site | Datadog US credentials |
| `DD_EU_API_KEY` / `DD_EU_APP_KEY` | — | Datadog EU credentials |
| `CLAUDE_BIN` | No | Path to Claude CLI binary (default: `claude`) |
| `CLAUDE_MAX_BUDGET` | No | Max USD per investigation (default: `2.00`) |
| `LINEAR_API_KEY` / `LINEAR_TEAM_ID` | Only for Linear tracker | Linear credentials |
| `NOTION_API_KEY` / `NOTION_DATABASE_ID` | Only for Notion doc sink | Notion credentials |
| `SLACK_WEBHOOK_URL` / `SLACK_CHANNEL_ID` | Only for Slack notifier | Slack credentials |
| `DASHBOARD_PORT` | No | Dashboard port (default: `8111`) |
| `NITPICK_INTERVAL` | No | Seconds between runs (default: `1800`) |
| `NITPICK_LOOKBACK` | No | How far back to search logs (default: `3600`) |
| `NITPICK_MAX_ERRORS_PER_RUN` | No | Max investigations per run (default: `10`) |
| `NITPICK_MAX_INVESTIGATIONS_PER_DAY` | No | Daily investigation cap (default: `50`) |
| `NITPICK_DRY_RUN` | No | Fetch and fingerprint only (default: `false`) |

### config.yml

```yaml
providers:
  source:    { type: datadog }    # Error log source
  doc_sink:  { type: markdown }   # Where RCA docs are written (markdown = local files)
  tracker:   { type: github }     # Where issues are filed (github = gh CLI)
  notifier:  { type: console }    # How you get notified (console = stdout)

services:
  my-api:
    datadog_query: "service:my-api status:error"
    regions: [us]                 # Datadog regions to search
    code_paths:                   # Where the source code lives
      - repo: backend
        path: src/my_api

repos:
  backend: /path/to/your/backend  # Local git clone

ignore_patterns:                  # Error messages matching these are skipped
  - "DeprecationWarning"
  - "health_check"
```

### Enabling optional providers

**Notion** (RCA documentation):
```yaml
providers:
  doc_sink: { type: notion }
```
Set `NOTION_API_KEY` and `NOTION_DATABASE_ID` in `.env`. Create a Notion database with properties: Title (title), Service (rich_text), Region (rich_text), Severity (rich_text), Fingerprint (rich_text).

**Linear** (issue tracking):
```yaml
providers:
  tracker: { type: linear }
```
Set `LINEAR_API_KEY` and `LINEAR_TEAM_ID` in `.env`. The team ID can be the short key (e.g. `ENG`) or UUID.

**Slack** (notifications):
```yaml
providers:
  notifier: { type: slack }
```
Set `SLACK_WEBHOOK_URL` and optionally `SLACK_CHANNEL_ID` in `.env`.

## Dashboard

Available at `http://localhost:8111` when running `python -m src.main serve`.

**Tabs:**
- **Errors** — All error groups with status, filterable by service/status. Click to expand details.
- **Investigations** — All Claude investigations with RCA, severity, fix status, cost, and links.
- **Runs** — Run history with stats (errors fetched, investigated, issues created, PRs, cost).
- **Costs** — Daily cost chart for the last 30 days.
- **Logs** — Live tail of the pipeline log.
- **Services** — Add/remove monitored services (edits `config.yml`).
- **Settings** — Toggle investigations, fixes, and notifications globally or per-service.

## Scheduling

### macOS LaunchAgent

Template plist files are included. Edit them to fill in `__INSTALL_DIR__` and `__UV_PATH__` placeholders, then:

```bash
cp com.example.nitpick.plist ~/Library/LaunchAgents/
cp com.example.nitpick-dashboard.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.nitpick.plist
launchctl load ~/Library/LaunchAgents/com.example.nitpick-dashboard.plist
```

### cron

```
0 * * * * cd /path/to/nitpick && .venv/bin/python -m src.main run >> logs/cron.log 2>&1
```

### systemd

Create a timer unit for the pipeline and a service unit for the dashboard.

## Cost controls

- **Per-investigation budget**: `CLAUDE_MAX_BUDGET` caps each Claude CLI invocation (default $2)
- **Per-run limit**: `NITPICK_MAX_ERRORS_PER_RUN` caps investigations per pipeline run (default 10)
- **Daily limit**: `NITPICK_MAX_INVESTIGATIONS_PER_DAY` caps total investigations per day (default 50)
- **Deduplication**: Same root cause is only investigated once — fingerprinting ensures recurring errors don't waste tokens
- **Fix budget**: Fix attempts have a separate $3 budget cap

## Project structure

```
├── config.yml                  # Services + provider config
├── .env                        # Credentials (not committed)
├── src/
│   ├── main.py                 # CLI + pipeline orchestrator
│   ├── config.py               # Env + YAML config loader
│   ├── models.py               # Dataclasses
│   ├── fingerprint.py          # Error normalisation + hashing
│   ├── cache.py                # SQLite dedup cache + dashboard queries
│   ├── investigator.py         # Claude CLI subprocess for investigation
│   ├── fixer.py                # Git worktree + Claude fix + PR creation
│   ├── dashboard.py            # Local HTTP server + API
│   └── providers/
│       ├── base.py             # Provider interfaces (Protocols)
│       ├── sources/datadog.py  # Datadog error source
│       ├── docsinks/markdown.py # Local Markdown RCA files
│       ├── docsinks/notion.py  # Notion RCA pages
│       ├── trackers/github.py  # GitHub Issues via gh CLI
│       ├── trackers/linear.py  # Linear issues via GraphQL
│       ├── notifiers/console.py # Console/log notifier
│       └── notifiers/slack.py  # Slack webhook notifier
├── static/
│   └── index.html              # Single-page dashboard UI
├── data/
│   └── error_cache.db          # SQLite database (created on first run)
└── logs/
    └── nitpick.log             # Pipeline logs
```

## License

MIT
