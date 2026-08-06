"""CLI entrypoints.

`python -m unc_notifier poll` runs one poll cycle and exits - the cron entrypoint.
Scheduling itself is left to the OS (cron / Task Scheduler) invoking this repeatedly at
a fixed interval - see SPEC.md. Jitter is a short random sleep at the start of a run so
concurrent installs don't all hit UNC's server in lockstep.

`python -m unc_notifier scrape-once` is a read-only smoke test for the real scraper:
fetches and prints one course's sections, never touches SmtpNotifier, Repository, or
the production database.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import UTC, datetime, timedelta

from unc_notifier.config import ConfigError, load_config, load_scraper_config, load_watches
from unc_notifier.logging_setup import configure_logging
from unc_notifier.models import CourseKey, SeatSnapshot
from unc_notifier.notifier.smtp import SmtpNotifier
from unc_notifier.poller import Poller, PollerConfig
from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.unc import UncClassSearchScraper
from unc_notifier.state.repository import Repository, connect

logger = logging.getLogger("unc_notifier.cli")


def _run_poll(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.env_file)
        watches = load_watches(config.watches_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_path)

    if not args.skip_jitter and config.jitter_seconds > 0:
        delay = random.uniform(0, config.jitter_seconds)
        logger.info("jitter_sleep seconds=%.1f", delay)
        time.sleep(delay)

    conn = connect(config.db_path)
    repository = Repository(conn)
    repository.ensure_schema()

    scraper = UncClassSearchScraper(contact_email=config.contact_email)
    notifier = SmtpNotifier(config.smtp)
    poller_config = PollerConfig(
        cooldown=_minutes(config.cooldown_minutes),
        default_waitlist_policy=config.default_waitlist_policy,
        circuit_breaker_threshold=config.circuit_breaker_threshold,
        backoff_base_seconds=config.backoff_base_seconds,
        backoff_max_seconds=config.backoff_max_seconds,
        notification_retry_delay=_minutes(config.notification_retry_delay_minutes),
    )
    poller = Poller(scraper, repository, notifier, poller_config)

    summary = poller.run(watches, now=datetime.now(UTC))
    logger.info(
        "poll_complete fetched=%d skipped=%d failed=%d observed=%d notified=%d "
        "notification_failures=%d",
        summary.courses_fetched,
        summary.courses_skipped_circuit_open,
        summary.courses_failed,
        summary.sections_observed,
        summary.sections_notified,
        summary.notification_failures,
    )
    print(
        f"courses_fetched={summary.courses_fetched} "
        f"courses_skipped_circuit_open={summary.courses_skipped_circuit_open} "
        f"courses_failed={summary.courses_failed} "
        f"sections_observed={summary.sections_observed} "
        f"sections_notified={summary.sections_notified} "
        f"notification_failures={summary.notification_failures}"
    )
    return 0


def _run_scrape_once(args: argparse.Namespace) -> int:
    """Read-only smoke test for the real scraper: GET + CSRF + POST + parse, for one
    course, printed to stdout. Deliberately never touches SmtpNotifier, Repository, or
    the production SQLite database - only load_scraper_config() and
    UncClassSearchScraper are used, so there is nothing here that can send email or
    write state. Uses load_scraper_config(), not load_config(): SMTP/database settings
    are neither read nor required, so this command works even before .env has any of
    that configured."""
    try:
        config = load_scraper_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    course_key = CourseKey(term=args.term, subject=args.subject, catalog_number=args.catalog_number)
    scraper = UncClassSearchScraper(contact_email=config.contact_email)

    try:
        snapshots = scraper.fetch_course(course_key)
    except FetchError as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a smoke test must never crash with a
        # raw traceback; report and exit non-zero like any other fetch failure.
        print(f"Fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _print_scrape_result(course_key, snapshots)
    return 0


def _print_scrape_result(course_key: CourseKey, snapshots: list[SeatSnapshot]) -> None:
    print(f"{course_key.subject} {course_key.catalog_number} ({course_key.term})")
    if not snapshots:
        print("  No sections found.")
        return
    for snapshot in snapshots:
        print(f"  {_format_snapshot(snapshot)}")


def _format_snapshot(snapshot: SeatSnapshot) -> str:
    key = snapshot.section_key
    if snapshot.waitlist_status_unknown:
        waitlist = "unknown"
    else:
        occupied = snapshot.waitlist_capacity - snapshot.waitlist_available
        waitlist = f"{occupied}/{snapshot.waitlist_capacity} occupied"
    return (
        f"section {key.section}: {snapshot.seats_available}/{snapshot.seats_capacity} "
        f"seats available (status={snapshot.status}, waitlist={waitlist})"
    )


def _minutes(value: int) -> timedelta:
    return timedelta(minutes=value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="unc-notifier")
    subparsers = parser.add_subparsers(dest="command", required=True)

    poll_parser = subparsers.add_parser("poll", help="Run one poll cycle and exit.")
    poll_parser.add_argument(
        "--env-file", default=None, help="Path to .env (default: .env in the current directory)"
    )
    poll_parser.add_argument(
        "--skip-jitter", action="store_true", help="Skip the startup jitter sleep."
    )
    poll_parser.set_defaults(func=_run_poll)

    scrape_once_parser = subparsers.add_parser(
        "scrape-once",
        help=(
            "Fetch and print one course's sections from the real UNC scraper. "
            "Read-only smoke test: no email, no observations, no state writes."
        ),
    )
    scrape_once_parser.add_argument(
        "--env-file", default=None, help="Path to .env (default: .env in the current directory)"
    )
    scrape_once_parser.add_argument("--term", required=True, help='e.g. "2026 Fall"')
    scrape_once_parser.add_argument("--subject", required=True, help='e.g. "COMP"')
    scrape_once_parser.add_argument(
        "--catalog-number", dest="catalog_number", required=True, help='e.g. "210"'
    )
    scrape_once_parser.set_defaults(func=_run_scrape_once)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
