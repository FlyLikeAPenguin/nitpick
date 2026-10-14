from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from .cache import ErrorCache
from .config import Config, load_config, validate_config
from .dashboard import run_dashboard
from .demo import demo_config, seed_demo_data
from .fingerprint import build_dd_link, fingerprint_and_group
from .fixer import attempt_fix, cleanup_stale_worktrees
from .investigator import investigate
from .models import ErrorGroup, Investigation, RunStats
from .providers import build_doc_sink, build_notifier, build_source, build_tracker

log = logging.getLogger("nitpick")


def setup_logging(config: Config) -> None:
    log_file = config.log_dir / "nitpick.log"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def recurrence_comment(group: ErrorGroup, investigation: Investigation) -> str:
    """Body of the note added to an existing issue when an error recurs."""
    lines = [
        "**Recurrence detected by nitpick** — same root cause as this issue, so no new issue was filed.",
        "",
        f"- Service: {group.service} ({group.region})",
        f"- Fingerprint: `{group.fingerprint}`",
        f"- Occurrences: {group.occurrence_count} since {group.first_seen}",
        f"- Severity: {investigation.severity}",
    ]
    if group.dd_query_link:
        lines.append(f"- [Datadog logs]({group.dd_query_link})")
    if investigation.notion_page_url:
        lines.append(f"- [RCA doc]({investigation.notion_page_url})")
    if investigation.root_cause:
        lines.extend(["", "## Root Cause", investigation.root_cause])
    if investigation.recommendation:
        lines.extend(["", "## Recommendation", investigation.recommendation])
    return "\n".join(lines)


def run_pipeline(config: Config) -> None:
    cache = ErrorCache(config.db_path)
    run_id = str(uuid4())
    run_started = datetime.now(timezone.utc)
    stats = RunStats()

    doc_sink = build_doc_sink(config)
    tracker = build_tracker(config)
    notifier = build_notifier(config)

    cleanup_stale_worktrees(config)

    from .pr_tracker import check_open_prs
    check_open_prs(cache, config, tracker=tracker, doc_sink=doc_sink)

    global_settings = cache.get_all_settings()
    max_per_run = int(global_settings.get("max_errors_per_run", str(config.max_errors_per_run)))
    max_per_day = int(global_settings.get("max_investigations_per_day", str(config.max_investigations_per_day)))

    log.info("=== nitpick run %s started ===", run_id[:8])
    log.info("Global settings: max_per_run=%d max_per_day=%d", max_per_run, max_per_day)

    if config.dry_run:
        log.info("DRY RUN mode — will fetch and fingerprint but not investigate/fix")

    investigated_this_run = 0
    limit_notified = False

    already_today = cache.count_investigations_today()
    if already_today >= max_per_day:
        log.info(
            "Daily investigation limit already reached, skipping investigations (%d/%d)",
            already_today, max_per_day,
        )

    for service_name, service_config in config.services.items():
        svc_settings = cache.resolve_service_settings(service_name)
        svc_investigations = svc_settings.get("investigations_enabled", "true") == "true"
        # Writing code is opt-in per service: absent setting means no fixes.
        # Investigation and issue-filing stay on by default; only the pipeline
        # stage that opens PRs against real repos requires explicit enabling.
        svc_fixes = svc_settings.get("fixes_enabled", "false") == "true"
        svc_notify = svc_settings.get("slack_enabled", "true") == "true"
        svc_docs = svc_settings.get("docs_enabled", "true") == "true"
        svc_issues = svc_settings.get("issues_enabled", "true") == "true"
        svc_budget = None
        if config.claude_max_budget is not None:
            svc_budget = float(
                svc_settings.get("investigation_budget", str(config.claude_max_budget))
            )

        log.info(
            "Service %s: investigations=%s docs=%s issues=%s fixes=%s notify=%s budget=%s",
            service_name, svc_investigations, svc_docs, svc_issues, svc_fixes,
            svc_notify,
            f"${svc_budget:.2f}" if svc_budget is not None else "uncapped",
        )

        for region in service_config.regions:
            source = build_source(config, region=region)
            if not source.is_configured:
                continue

            raw_logs = source.fetch(
                query=service_config.datadog_query,
                lookback_seconds=config.lookback,
                limit=200,
            )
            stats.errors_fetched += len(raw_logs)
            time.sleep(0.5)

            groups = fingerprint_and_group(
                raw_logs, service_name, region, config.ignore_patterns
            )

            for group in groups:
                from_ts = group.first_seen.strftime("%s000")
                to_ts = group.last_seen.strftime("%s000")
                group.dd_query_link = build_dd_link(
                    region, service_config.datadog_query, from_ts, to_ts
                )

                is_new = cache.upsert_error(group)
                if is_new:
                    stats.new_errors += 1

                needs_investigation = is_new or cache.get_status(group.fingerprint) in ("new", "failed")
                if not needs_investigation:
                    continue

                if config.dry_run:
                    log.info(
                        "[DRY RUN] New error: %s/%s fp=%s count=%d",
                        service_name, group.error_type,
                        group.fingerprint, group.occurrence_count,
                    )
                    continue

                if not svc_investigations:
                    log.info("Investigations disabled for %s, skipping", service_name)
                    continue

                if investigated_this_run >= max_per_run:
                    log.info("Max errors per run (%d) reached, deferring", max_per_run)
                    break

                # Counted fresh rather than from a snapshot taken at run start:
                # a run can outlast the UTC day (investigations are slow and the
                # host sleeps mid-run), and a stale count charges yesterday's
                # usage against today's budget — halting a run that still has
                # its full quota. Re-reading also lets a run that crossed
                # midnight resume once the new day's budget opens up.
                investigations_today = cache.count_investigations_today()
                if investigations_today >= max_per_day:
                    if not limit_notified:
                        log.info(
                            "Daily investigation limit reached (%d/%d)",
                            investigations_today, max_per_day,
                        )
                        if notifier:
                            notifier.notify_daily_limit(investigations_today, max_per_day)
                        limit_notified = True
                    break
                limit_notified = False

                svc_config_copy = config.__class__(**config.__dict__)
                svc_config_copy.claude_max_budget = svc_budget

                cache.mark_investigating(group.fingerprint, "")
                recent_investigations = cache.get_recent_investigations_for_service(service_name)
                investigation = investigate(group, service_config, svc_config_copy, recent_investigations)
                cache.save_investigation(investigation)
                stats.investigated += 1
                stats.total_cost_usd += investigation.claude_cost_usd
                investigated_this_run += 1

                if investigation.status == "failed":
                    cache.update_error_status(group.fingerprint, "failed")
                    continue

                new_status = "documented"

                # Create RCA documentation via doc sink
                if svc_docs and doc_sink and doc_sink.is_configured:
                    try:
                        doc_url = doc_sink.create(group, investigation)
                        investigation.notion_page_url = doc_url
                    except Exception as e:
                        log.warning("Doc sink create failed: %s", e)

                # File issue via tracker. An error whose root cause is already
                # tracked gets a recurrence note on the existing issue instead
                # of a new one — the same error checked twice must not produce
                # two tickets.
                #
                # Only the filing of a NEW issue waits on is_actionable. Linking
                # a recurrence does not: the two verdicts are independent (the
                # investigator is told so), and the same error readily lands on
                # either side of the actionable line between runs. Gating both
                # on it dropped those recurrences silently — no comment on the
                # issue, no count — losing the recurrence record for an error
                # already known to be worth tracking.
                linked_existing = False
                if svc_issues and tracker and tracker.is_configured:
                    if investigation.is_duplicate:
                        try:
                            tracker.comment(
                                investigation.duplicate_of_url,
                                recurrence_comment(group, investigation),
                            )
                            investigation.linear_issue_url = investigation.duplicate_of_url
                            linked_existing = True
                            stats.duplicates_linked += 1
                            log.info(
                                "Duplicate of %s — commented instead of filing (fp=%s)",
                                investigation.duplicate_of_url, group.fingerprint,
                            )
                        except Exception as e:
                            log.warning("Tracker comment failed: %s", e)
                    elif investigation.is_actionable:
                        try:
                            issue_url = tracker.create(group, investigation)
                            investigation.linear_issue_url = issue_url
                            if issue_url:
                                stats.issues_created += 1
                        except Exception as e:
                            log.warning("Tracker create failed: %s", e)

                # Attempt fix if feasible. Duplicates are excluded: the tracked
                # issue owns the fix, and a second PR would compete with it.
                if (
                    svc_fixes
                    and not investigation.is_duplicate
                    and investigation.is_fixable
                    and investigation.estimated_fix_lines
                    and investigation.estimated_fix_lines < 250
                ):
                    pr_url = attempt_fix(group, investigation, service_config, config)
                    if pr_url:
                        stats.prs_created += 1
                        stats.total_cost_usd += investigation.claude_cost_usd
                        new_status = "fixed"

                        # Attach PR to issue and add doc comment
                        if tracker and investigation.linear_issue_url:
                            try:
                                short_desc = investigation.fix_description[:60] if investigation.fix_description else investigation.root_cause[:60]
                                tracker.attach_pr(investigation.linear_issue_url, pr_url, f"fix({group.service}): {short_desc}")
                                tracker.transition(investigation.linear_issue_url, "in_review")
                            except Exception as e:
                                log.warning("Tracker PR attach failed: %s", e)
                        if doc_sink and investigation.notion_page_url:
                            try:
                                doc_sink.comment(investigation.notion_page_url, f"Fix PR opened: {pr_url}")
                            except Exception as e:
                                log.warning("Doc sink comment failed: %s", e)

                cache.save_investigation(investigation)
                cache.update_error_status(group.fingerprint, new_status)

                # Notify. Recurrences of an already-tracked error stay quiet —
                # the issue they were added to was announced when it was filed.
                if svc_notify and notifier and investigation.is_actionable and not linked_existing:
                    subscribers = cache.get_subscribers_for_service(service_name)
                    notifier.notify(group, investigation, config, subscribers)

    run_completed = datetime.now(timezone.utc)
    cache.log_run(run_id, run_started, run_completed, stats.as_dict())

    log.info(
        "=== Run %s complete: fetched=%d new=%d investigated=%d issues=%d dupes=%d prs=%d cost=$%.4f ===",
        run_id[:8], stats.errors_fetched, stats.new_errors,
        stats.investigated, stats.issues_created, stats.duplicates_linked,
        stats.prs_created, stats.total_cost_usd,
    )


def cli() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m src.main <run|serve|demo>")
        print("  run   — execute one pipeline cycle")
        print("  serve — start the monitoring dashboard")
        print("  demo  — seed a credential-free dashboard demo")
        sys.exit(1)

    config = load_config()
    setup_logging(config)

    command = sys.argv[1]

    if command == "run":
        missing = validate_config(config)
        if missing:
            log.warning("Config warnings: %s", ", ".join(missing))
        run_pipeline(config)

    elif command == "serve":
        run_dashboard(config)

    elif command == "demo":
        demo_cfg = demo_config(config)
        seed_demo_data(demo_cfg.db_path)
        print(f"Demo data ready at {demo_cfg.db_path}")
        run_dashboard(demo_cfg)

    else:
        print(f"Unknown command: {command}")
        print("Usage: python -m src.main <run|serve|demo>")
        sys.exit(1)


if __name__ == "__main__":
    cli()
