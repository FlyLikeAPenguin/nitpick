# Nitpick — Agent Context

## What This Is

Automated error monitoring and investigation. Fetches errors from a configured error source (Datadog by default), deduplicates via SHA256 fingerprinting, investigates with Claude CLI, documents findings via a pluggable doc sink, files issues via a pluggable tracker, optionally attempts automated fixes via git worktree + PR, and sends notifications via a pluggable notifier. Includes a local web dashboard at localhost:8111.

## Architecture

```
Error Source  →  Fingerprint + Dedup (SQLite)  →  Claude CLI Investigation
(Datadog)                                          ↓
                                         ┌─────────┼──────────┐
                                         ↓         ↓          ↓
                                     Doc Sink   Tracker    Notifier
                                    (Markdown/  (GitHub/   (Console/
                                     Notion)    Linear)     Slack)
                                         ↓
                                    Fix Attempt
                                   (worktree + PR)

SQLite  ←──────────────────────────────────→  Web Dashboard (localhost:8111)
```

## Provider Model

Four pluggable roles configured in `config.yml` under `providers:`:

| Role | Default (zero-SaaS) | Alternative | What it does |
|---|---|---|---|
| `source` | `datadog` | — | Fetches error logs |
| `doc_sink` | `markdown` | `notion` | Creates RCA documentation |
| `tracker` | `github` | `linear` | Creates/manages issues |
| `notifier` | `console` | `slack` | Sends notifications |

Each provider is guarded by `is_configured` — if credentials are missing, it's skipped gracefully.

## Critical Design Decisions

### Claude CLI invocation
Claude is invoked as a subprocess via `subprocess.run()` with `input=prompt` (stdin). Uses `--output-format json` to get a parseable envelope. The prompt instructs Claude to output a JSON object with investigation results. Claude is engine-only — it does NOT create issues or documentation; those are created by Python provider adapters after investigation returns.

### Direct API for post-investigation operations
For operations after Claude finishes (creating docs, filing issues, attaching PRs, transitioning status), Python calls provider adapters directly — NOT additional Claude subprocess calls. This was an explicit decision: spawning Claude for simple API calls is wasteful, and using Python adapters makes providers pluggable.

### PR lifecycle
1. Claude creates fix in git worktree → push → `gh pr create`
2. Python attaches PR to issue via tracker adapter, transitions to "in_review"
3. Python adds comment to RCA doc via doc sink adapter
4. Each pipeline run checks open PRs via `gh pr view --json`
5. On merge: DB→fixed, tracker→done, doc comment "Fix merged"
6. On close: DB→documented, doc comment "Fix PR closed"

### Dashboard settings override pipeline config
The `settings` table in SQLite holds runtime-adjustable values (investigations_enabled, docs_enabled, issues_enabled, fixes_enabled, notifications_enabled, max_errors_per_run, max_investigations_per_day, investigation_budget). The pipeline reads these at the start of each run and they override the env var defaults. The dashboard UI provides toggles/inputs for all of these.

### Pipeline stages are independently toggleable
Investigate, document, file, fix and notify are gated separately, globally and per-service, so a deployment can run any subset. Turning `docs_enabled` off leaves the tracker issue as the RCA of record — the issue body carries every section the doc sink would have written (root cause, recommendation, fix description, affected files), so nothing is lost. Turning `issues_enabled` off keeps the doc without a ticket. Provider `type` in config.yml chooses *which* adapter fills a role; these settings choose *whether* that stage runs at all.

### config.yml is the source of truth for services
Services (queries, regions, code paths) live in `config.yml`, not the database. The dashboard reads and writes this file directly via `yaml.safe_load`/`yaml.dump`.

## Known Gotchas

### Schema migrations
`cache.py` has a `_migrate()` method that runs `ALTER TABLE` for columns added after initial release. Any new columns need a migration added here.

### Investigation VALUES count
`save_investigation()` inserts a row with a specific number of VALUES placeholders. When adding fields to the Investigation model, update both the INSERT statement and the field count.

### Claude output parsing
Claude often explains its finding before emitting the requested JSON, and that prose regularly contains braces of its own — Go panic frames like `{0x0, 0x0, 0x0}` are the common case, since a brace-heavy stack is exactly what an investigation quotes back. `_extract_json()` in `investigator.py` therefore decodes *every* brace-led candidate with `raw_decode` and keeps the one carrying the most keys of the requested schema, breaking ties on length so an outer object beats a nested one. This subsumes code-fence handling — a fenced object is found by its brace like any other. An earlier version sliced from the first `{` to the last `}`, which swallowed the prose and discarded otherwise complete investigations at ~17% on Go services against ~1% elsewhere.

### Investigations fail loudly
A dropped connection still returns a well-formed CLI envelope with the failure in the result text (`API Error: Connection closed mid-response`), so it would otherwise parse as an ordinary unactionable result. `parse_claude_output()` detects this via `is_error` or the error-text prefix. Anything that yields no usable result — API error, unparseable output, no output — is retried once (`MAX_ATTEMPTS`), then recorded with `status='failed'` and a `failure_reason`, which puts the error group back to `failed` so the next run re-investigates it rather than closing it with a result nobody produced. `failure_reason` is surfaced on the dashboard; without it a failure is indistinguishable from a genuine "no action needed".

### Dedup context for investigations
When investigating, the pipeline passes recent investigations for the same service so Claude can recognise an error it has already seen. If the error shares a root cause with an existing issue — one from that list or one Claude finds itself — it sets `is_duplicate=true` and `duplicate_of_url`, and the pipeline posts a recurrence note on that issue via `tracker.comment()` instead of filing a new one. Duplicates also skip the fix stage and the Slack notification.

`is_duplicate` is deliberately independent of `is_actionable`: a duplicate of a real, unfixed bug is still actionable, so `is_actionable` cannot be used to suppress the ticket. A duplicate claim with no `duplicate_of_url` is downgraded to a normal filing — there would be nowhere to record the recurrence.

## Stack

- **Python 3.12+**, minimal deps: `requests`, `pyyaml`, `python-dotenv`, stdlib `sqlite3`
- **Claude CLI**: shelled out as subprocess, `--output-format json`
- **Dashboard**: Python stdlib `http.server`, single-file vanilla HTML/JS/CSS (`static/index.html`)
- **Storage**: SQLite at `data/error_cache.db`
- **Scheduling**: macOS LaunchAgent (StartCalendarInterval for pipeline, KeepAlive for dashboard), or cron/systemd

## File Map

| File | Purpose |
|---|---|
| `src/main.py` | CLI entry + pipeline orchestrator |
| `src/config.py` | Env + config.yml loader → `Config` dataclass |
| `src/models.py` | `ErrorGroup`, `Investigation`, `RunStats`, `ServiceConfig` |
| `src/cache.py` | SQLite: all DB operations, schema, migrations, settings |
| `src/fingerprint.py` | Error normalization + SHA256 grouping |
| `src/investigator.py` | Claude CLI subprocess for investigation |
| `src/fixer.py` | Git worktree + Claude fix + PR creation |
| `src/providers/` | Pluggable provider interfaces and adapters |
| `src/dashboard.py` | HTTP server + API endpoints |
| `static/index.html` | Single-page dashboard UI |
| `config.yml` | Service definitions + provider config |

## Environment Variables

See `.env.example`. Key ones: `DD_US_API_KEY`, `DD_US_APP_KEY`, `CLAUDE_BIN`, `CLAUDE_MAX_BUDGET`. Optional: `LINEAR_API_KEY`, `LINEAR_TEAM_ID`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `SLACK_WEBHOOK_URL`.

## Testing / Running

```bash
# Dry run (fetch + fingerprint, no investigation/fix)
NITPICK_DRY_RUN=true python -m src.main run

# Single investigation
NITPICK_MAX_ERRORS_PER_RUN=1 python -m src.main run

# Dashboard
python -m src.main serve
```
