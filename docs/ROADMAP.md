# Roadmap

Nitpick is moving toward a source-agnostic, reviewable incident-workflow layer: bring errors in from the systems a team already uses, investigate them with the agent backend it prefers, and send trustworthy findings to the places where work already happens.

The roadmap is directional rather than a promise of dates. Adoption feedback, security, and operational reliability can change the order. Completed work is kept visible so the project’s progress is easy to assess.

## Product direction

```text
More log sources → one normalized incident model → more agent backends
                                      ↓
                         docs, issues, notifications, fix PRs
                                      ↓
                    local-first today → deployable tomorrow
```

The design principles stay constant as the surface area grows:

- **Reviewable automation** — investigations explain their evidence; changes arrive as PRs.
- **Provider choice** — sources, agent backends, and sinks should be replaceable adapters.
- **Operational control** — budgets, permissions, retention, and approval gates remain visible.
- **Deploy anywhere** — start locally, then support dependable self-hosted operation.

## Shipped in v0.1.0

- Datadog log ingestion with fingerprinting and SQLite deduplication.
- Claude CLI investigation with cost limits and structured RCA output.
- Markdown and Notion RCA sinks; GitHub and Linear issue trackers; console and Slack notifications.
- Optional fix PRs in isolated git worktrees, disabled by default.
- Local dashboard, credential-free demo mode, tests, CI, release packaging, and contributor/security guidance.

## Now

The next focus is making the existing local workflow dependable enough for real teams:

- Expand provider contract tests, cache migration coverage, and sanitized log fixtures.
- Improve dashboard accessibility, filtering, run history, and error-to-investigation navigation.
- Make privacy, retention, permissions, and cost behavior configurable and easy to audit.
- Improve setup and scheduling documentation for macOS, Linux, and repeatable upgrades.

## Next

### More sources

- Add Sentry, OpenTelemetry/OTLP, and CloudWatch adapters behind the existing source interface.
- Support webhook and fixture inputs so teams can integrate systems without a bespoke polling loop.
- Preserve one normalized error model so downstream investigations do not depend on the source vendor.

### More sinks and workflow integrations

- Add Jira and additional issue/project-management adapters.
- Add richer incident destinations such as PagerDuty-style notifications and generic webhooks.
- Improve document output with linked timelines, evidence sections, and reusable RCA templates.

### More agent backends

- Add first-class Codex support alongside Claude CLI.
- Introduce a provider-neutral agent runner with explicit capability, permission, and cost contracts.
- Make model selection and investigation prompts configurable without coupling the pipeline to one vendor.

### Deployability

- Provide a documented Docker/Compose path for the pipeline and dashboard.
- Add production-oriented systemd and launchd examples with health checks and log rotation.
- Make database, logs, configuration, and secrets paths explicit so Nitpick can run cleanly in a container or worker host.

## Later

- Add richer incident timelines, related-error grouping, and cross-service correlation.
- Add approval gates before issue creation, external notifications, or fix-PR creation.
- Support a GitHub App integration so teams can minimize local CLI coupling and scope permissions precisely.
- Evaluate a hosted or multi-user deployment model only after self-hosted operation is stable and privacy boundaries are proven.

## How priorities are chosen

We prioritize work that reduces the time from a real error to a reviewed, well-documented action. Issues with sanitized examples, measurable user impact, and a clear privacy or operational model are especially useful. [Open an issue](https://github.com/FlyLikeAPenguin/nitpick/issues/new/choose) with the problem and desired outcome.
