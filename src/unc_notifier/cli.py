"""Cron entrypoint: `python -m unc_notifier poll` runs one poll cycle and exits.

Scheduling itself is left to the OS (cron / Task Scheduler) invoking this repeatedly at
a fixed interval - see SPEC.md. Jitter is a short random sleep at the start of a run so
concurrent installs don't all hit UNC's server in lockstep.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import UTC, datetime, timedelta

from unc_notifier.config import ConfigError, load_config, load_watches
from unc_notifier.logging_setup import configure_logging
from unc_notifier.notifier.smtp import SmtpNotifier
from unc_notifier.poller import Poller, PollerConfig
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
