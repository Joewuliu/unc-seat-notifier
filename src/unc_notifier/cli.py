"""CLI entrypoints.

`python -m unc_notifier poll` runs one poll cycle and exits - the cron entrypoint.
Scheduling itself is left to the OS (cron / Task Scheduler) invoking this repeatedly at
a fixed interval - see SPEC.md. Jitter is a short random sleep at the start of a run so
concurrent installs don't all hit UNC's server in lockstep.

`python -m unc_notifier scrape-once` is a read-only smoke test for the real scraper:
fetches and prints one course's sections, never touches SmtpNotifier, Repository, or
the production database.

`python -m unc_notifier poll-once-dry-run` exercises the real scraper, the real pure
state machine (diff.evaluate), and a real Repository together for one section - but
only against an explicitly supplied --db-path, and prints notifications to the console
instead of sending email via SmtpNotifier.

`python -m unc_notifier test-email` sends exactly one clearly-labeled test email via
the real SmtpNotifier, to verify SMTP configuration. Never scrapes UNC, reads
watches.yaml, or touches SQLite.

`python -m unc_notifier poll-once-live` runs the real end-to-end poll (real scraper,
real Repository, real Poller.apply_snapshot(), real SmtpNotifier) for one section
against an explicitly supplied --db-path, gated behind --confirm-send-email. Its
--seed-closed-state mode (gated behind --confirm-seed) writes a synthetic prior closed
state with no network call, so a later live run can demonstrate a real closed-to-open
email.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from unc_notifier import diff
from unc_notifier.config import (
    AppConfig,
    ConfigError,
    load_config,
    load_scraper_config,
    load_smtp_config,
    load_watches,
)
from unc_notifier.lock import LockUnavailable, single_instance_lock
from unc_notifier.logging_setup import configure_logging
from unc_notifier.models import (
    CourseKey,
    Decision,
    NotificationEvent,
    SeatSnapshot,
    SectionKey,
    SectionState,
    WaitlistPolicy,
    WatchEntry,
)
from unc_notifier.notifier.console import ConsoleNotifier
from unc_notifier.notifier.smtp import SmtpConfig, SmtpNotifier
from unc_notifier.poller import Poller, PollerConfig, PollSummary
from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.unc import UncClassSearchScraper
from unc_notifier.state.repository import Repository, connect

# Fixed defaults for poll-once-dry-run and poll-once-live, neither of which has a
# PollerConfig of its own - these are single-section diagnostic tools, not a
# configurable production pipeline.
_SINGLE_SECTION_COOLDOWN = timedelta(minutes=30)
_SINGLE_SECTION_RETRY_DELAY = timedelta(minutes=5)
_SINGLE_SECTION_WAITLIST_POLICY = WaitlistPolicy.ALERT

logger = logging.getLogger("unc_notifier.cli")


def _run_poll(args: argparse.Namespace) -> int:
    """The real end-to-end poll cycle - the cron/Task Scheduler entrypoint. Prints and
    logs the resolved (non-secret) production paths at startup so a run launched from
    an unfamiliar working directory (as Task Scheduler does) is always traceable, and
    guards the whole cycle with single_instance_lock() so a second scheduled run that
    overlaps a still-running one exits immediately instead of racing it."""
    run_started_at = datetime.now(UTC)
    try:
        config = load_config(args.env_file)
        watches = load_watches(config.watches_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(config.log_path)
    print(f"Config file: {args.env_file or '(default .env in current directory)'}")
    print(f"Database: {config.db_path}")
    print(f"Log file: {config.log_path}")
    print(f"Watches file: {config.watches_path}")
    print(f"Lock file: {config.lock_path}")
    print(f"Watched entries: {len(watches)}")
    logger.info(
        "run_start timestamp=%s watched_entries=%d db_path=%s log_path=%s "
        "watches_path=%s lock_path=%s",
        run_started_at.isoformat(), len(watches), config.db_path, config.log_path,
        config.watches_path, config.lock_path,
    )

    try:
        with single_instance_lock(config.lock_path):
            exit_code = _run_poll_locked(args, config, watches)
    except LockUnavailable as exc:
        logger.warning("run_skipped_overlap error=%s", exc)
        print(f"Another poll is already running - exiting cleanly ({exc}).")
        return 0

    logger.info("run_end exit_status=%d", exit_code)
    return exit_code


def _run_poll_locked(
    args: argparse.Namespace, config: AppConfig, watches: list[WatchEntry]
) -> int:
    """The part of the poll cycle that must only ever run under single_instance_lock()."""
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


def _run_production_check(args: argparse.Namespace) -> int:
    """Read-only validation that everything `poll` would need is present and
    structurally valid, without ever scraping UNC, sending email, or creating or
    modifying the production database - the only side effect anywhere in this
    function is a temp-file write-and-delete probe used to confirm a directory is
    writable, immediately cleaned up."""
    print(f"Python: {sys.executable} ({sys.version.split()[0]})")
    print(f"Virtual environment active: {sys.prefix != sys.base_prefix}")

    checks: list[tuple[str, bool, str]] = []

    env_display = args.env_file or "(default .env in current directory)"
    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        checks.append(("Configuration parses", False, f"{env_display}: {exc}"))
        _print_checks(checks)
        return 1
    checks.append(("Configuration parses", True, env_display))

    if not config.watches_path.exists():
        checks.append(("Watches file exists", False, str(config.watches_path)))
    else:
        checks.append(("Watches file exists", True, str(config.watches_path)))
        try:
            watches = load_watches(config.watches_path)
        except ConfigError as exc:
            checks.append(("Watches file parses", False, str(exc)))
        else:
            checks.append(("Watches file parses", True, f"{len(watches)} entries"))

    checks.append(("Database directory writable", *_check_dir_writable(config.db_path.parent)))
    checks.append(("Log directory writable", *_check_dir_writable(config.log_path.parent)))
    checks.append(("Lock directory writable", *_check_dir_writable(config.lock_path.parent)))
    checks.append(
        ("SMTP settings structurally valid", *_check_smtp_structurally_valid(config.smtp))
    )
    checks.append(
        ("Scraper contact email present", bool(config.contact_email), config.contact_email)
    )

    _print_checks(checks)
    return 0 if all(passed for _, passed, _ in checks) else 1


def _check_dir_writable(directory: Path) -> tuple[bool, str]:
    """The directory itself doesn't have to exist yet - connect()/configure_logging()/
    single_instance_lock() all create their target directory lazily on first real
    run via mkdir(parents=True). What actually matters for readiness is whether the
    nearest *existing* ancestor can have new entries created inside it; this never
    creates the target directory itself, only a temp probe file (immediately
    deleted) in whichever existing ancestor it finds."""
    probe_target = directory
    while not probe_target.exists():
        parent = probe_target.parent
        if parent == probe_target:
            return False, f"no existing ancestor directory found for {directory}"
        probe_target = parent

    if not probe_target.is_dir():
        return False, f"not a directory: {probe_target}"
    try:
        with tempfile.NamedTemporaryFile(dir=probe_target, delete=True):
            pass
    except OSError as exc:
        return False, f"not writable: {exc}"

    if probe_target == directory:
        return True, str(directory)
    return True, f"{directory} (will be created on first run; {probe_target} is writable)"


def _check_smtp_structurally_valid(smtp: SmtpConfig) -> tuple[bool, str]:
    """Structural validation only - never opens a socket or connects to the SMTP
    host. Detail text never includes username/password."""
    problems = []
    if not smtp.host:
        problems.append("SMTP_HOST is empty")
    if not (1 <= smtp.port <= 65535):
        problems.append(f"SMTP_PORT {smtp.port} out of range")
    if "@" not in smtp.from_addr:
        problems.append("SMTP_FROM does not look like an email address")
    if "@" not in smtp.to_addr:
        problems.append("SMTP_TO does not look like an email address")
    if bool(smtp.username) != bool(smtp.password):
        problems.append("SMTP_USERNAME and SMTP_PASSWORD must both be set or both omitted")

    if problems:
        return False, "; ".join(problems)
    return True, f"{smtp.host}:{smtp.port} (TLS={'on' if smtp.use_tls else 'off'})"


def _print_checks(checks: list[tuple[str, bool, str]]) -> None:
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")


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


def _run_test_email(args: argparse.Namespace) -> int:
    """Sends exactly one clearly-labeled configuration-test email via the real
    SmtpNotifier. Uses load_smtp_config(), not load_config(): CONTACT_EMAIL,
    watches.yaml, and database settings are neither read nor required. Never imports
    or constructs UncClassSearchScraper, Repository, Poller, or a SQLite connection -
    there is nothing in this function's code path capable of touching any of them."""
    try:
        smtp_config = load_smtp_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Sending test email via {smtp_config.host}:{smtp_config.port} "
        f"to {smtp_config.to_addr}..."
    )
    try:
        SmtpNotifier(smtp_config).send_test_email()
    except Exception as exc:  # noqa: BLE001 - report cleanly, never a raw traceback
        message = _redact_secrets(f"{type(exc).__name__}: {exc}", smtp_config)
        print(f"SMTP test email failed: {message}", file=sys.stderr)
        return 1

    # Only printed once send_test_email() has returned without raising - smtplib's
    # send_message() itself raises on anything the server didn't accept, so reaching
    # here means the server actually confirmed the send.
    print("Test email sent successfully.")
    return 0


def _redact_secrets(text: str, config: SmtpConfig) -> str:
    """Never let SMTP_USERNAME/SMTP_PASSWORD reach printed output, even indirectly via
    an exception's string representation (e.g. a relay echoing back what it received)."""
    redacted = text
    for secret in (config.password, config.username):
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


def _run_poll_once_dry_run(args: argparse.Namespace) -> int:
    """Exercises the real scraper, the real pure state machine (diff.evaluate), and a
    real Repository together for exactly one section, printing every step.

    Never imports or constructs SmtpNotifier - ConsoleNotifier prints instead of
    sending. Never opens any database other than the one explicitly passed via
    --db-path (argparse makes that flag required, so there is no default to fall back
    to). Safe to run repeatedly: each run just diffs against whatever this exact
    --db-path already has stored, the same guarantee the real poll command relies on.
    """
    try:
        config = load_scraper_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    section_key = SectionKey(
        term=args.term, subject=args.subject, catalog_number=args.catalog_number,
        section=args.section,
    )
    course_key = section_key.course_key

    scraper = UncClassSearchScraper(contact_email=config.contact_email)
    try:
        snapshots = scraper.fetch_course(course_key)
    except FetchError as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - report cleanly, never a raw traceback
        print(f"Fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    snapshot = next((s for s in snapshots if s.section_key == section_key), None)
    if snapshot is None:
        print(
            f"Section {args.section} not found in the response for "
            f"{course_key.subject} {course_key.catalog_number} ({course_key.term}).",
            file=sys.stderr,
        )
        return 1

    print(f"Fetched seat state: {_format_snapshot(snapshot)}")

    conn = connect(args.db_path)
    repository = Repository(conn)
    repository.ensure_schema()

    prior_state = repository.get_section_state(section_key)
    print(f"Previous stored state: {_format_state(prior_state)}")

    # Same rule the real poller uses: a row holding only failure bookkeeping (never a
    # successful observation) must be treated as "no prior state," not real history.
    prev_for_diff = (
        prior_state if (prior_state is not None and prior_state.last_seen_at) else None
    )
    was_available = prev_for_diff.is_available if prev_for_diff is not None else None
    print(f"Detected transition: {_format_transition(was_available, snapshot.is_available)}")

    decision = diff.evaluate(
        prev_for_diff,
        snapshot,
        waitlist_policy=_SINGLE_SECTION_WAITLIST_POLICY,
        cooldown=_SINGLE_SECTION_COOLDOWN,
        retry_delay=_SINGLE_SECTION_RETRY_DELAY,
    )
    print(f"Notification decision: reason={decision.reason} level={decision.notify_level or '-'}")
    print(f"Would notify: {decision.should_attempt_notify}")

    final_state = decision.new_state
    if decision.should_attempt_notify:
        final_state = _attempt_console_notify(snapshot, decision, now=datetime.now(UTC))

    repository.save_state(final_state)
    repository.record_observation(snapshot)

    print(f"Final stored state: {_format_state(final_state)}")
    return 0


def _attempt_console_notify(
    snapshot: SeatSnapshot, decision: Decision, *, now: datetime
) -> SectionState:
    """Mirrors Poller._attempt_notify's success/failure state resolution exactly, using
    ConsoleNotifier in place of the real notifier. Kept separate from Poller itself so
    this dry-run tool never risks touching the production poll pipeline's code path."""
    event = NotificationEvent(
        section_key=snapshot.section_key,
        seats_available=snapshot.seats_available,
        seats_capacity=snapshot.seats_capacity,
        waitlist_available=snapshot.waitlist_available,
        waitlist_capacity=snapshot.waitlist_capacity,
        status=snapshot.status,
        reason=decision.reason,
        level=decision.notify_level or diff.LEVEL_INFO,
        occurred_at=snapshot.observed_at,
    )
    try:
        ConsoleNotifier().notify(event)
    except Exception as exc:  # noqa: BLE001 - never let a notify failure crash the tool
        print(f"Notification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return replace(decision.new_state, last_notification_attempt_at=now)

    return replace(
        decision.new_state,
        last_notified_at=now,
        pending_notification_since=None,
        last_notification_attempt_at=now,
    )


def _format_state(state: SectionState | None) -> str:
    if state is None:
        return "none (never observed)"
    return (
        f"seats={state.seats_available}/{state.seats_capacity} status={state.status} "
        f"last_notified_at={state.last_notified_at} "
        f"pending_notification_since={state.pending_notification_since}"
    )


def _format_transition(was_available: bool | None, is_available: bool) -> str:
    if was_available is None:
        return f"first observation (now {'available' if is_available else 'unavailable'})"
    if was_available == is_available:
        return f"no change ({'available' if is_available else 'unavailable'})"
    return "unavailable -> available" if is_available else "available -> unavailable"


def _run_poll_once_live(args: argparse.Namespace) -> int:
    """Runs the real end-to-end poll for exactly one section: real
    UncClassSearchScraper, real Repository, real Poller.apply_snapshot() (so
    notification-resolution logic is never re-implemented here - see
    PROJECT_STATUS.md), and the real SmtpNotifier.

    Requires --db-path (no default - never falls back to the production database) and,
    for a real live run, --confirm-send-email: without it the command refuses to run
    at all, before fetching anything or constructing SmtpNotifier.
    --seed-closed-state is a separate, network-free test-setup mode gated by its own
    --confirm-seed flag - see _seed_closed_state().
    """
    print(f"Database: {args.db_path}")
    section_key = SectionKey(
        term=args.term, subject=args.subject, catalog_number=args.catalog_number,
        section=args.section,
    )

    if args.seed_closed_state:
        return _seed_closed_state(args, section_key)

    if not args.confirm_send_email:
        print(
            "Refusing to run: poll-once-live can send a real email via SmtpNotifier. "
            "Re-run with --confirm-send-email to proceed, or use poll-once-dry-run "
            "for a safe preview that never sends email.",
            file=sys.stderr,
        )
        return 1

    # Never let Poller's internal logging (e.g. a notification_failed record, which
    # includes the raw notifier exception text) reach the console via Python's default
    # "no handler configured" stderr fallback - this command's own print() statements,
    # redacted where relevant, are the only sanctioned output.
    logging.getLogger("unc_notifier").addHandler(logging.NullHandler())

    try:
        scraper_config = load_scraper_config(args.env_file)
        smtp_config = load_smtp_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    course_key = section_key.course_key
    scraper = UncClassSearchScraper(contact_email=scraper_config.contact_email)
    try:
        snapshots = scraper.fetch_course(course_key)
    except FetchError as exc:
        print(f"Fetch failed: {_redact_secrets(str(exc), smtp_config)}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - report cleanly, never a raw traceback
        message = _redact_secrets(f"{type(exc).__name__}: {exc}", smtp_config)
        print(f"Fetch failed: {message}", file=sys.stderr)
        return 1

    snapshot = next((s for s in snapshots if s.section_key == section_key), None)
    if snapshot is None:
        print(
            f"Section {args.section} not found in the response for "
            f"{course_key.subject} {course_key.catalog_number} ({course_key.term}).",
            file=sys.stderr,
        )
        return 1

    print(f"Fetched seat state: {_format_snapshot(snapshot)}")

    conn = connect(args.db_path)
    repository = Repository(conn)
    repository.ensure_schema()

    prior_state = repository.get_section_state(section_key)
    print(f"Previous stored state: {_format_state(prior_state)}")

    # Same rule Poller.apply_snapshot() applies internally: a row holding only failure
    # bookkeeping (never a successful observation) is "no prior state," not history.
    prev_for_diff = (
        prior_state if (prior_state is not None and prior_state.last_seen_at) else None
    )
    was_available = prev_for_diff.is_available if prev_for_diff is not None else None
    print(f"Detected transition: {_format_transition(was_available, snapshot.is_available)}")

    watch_entry = WatchEntry(
        term=args.term, subject=args.subject, catalog_number=args.catalog_number,
        section=args.section,
    )
    notifier = SmtpNotifier(smtp_config)
    poller = Poller(scraper, repository, notifier, _single_section_poller_config())
    summary = PollSummary()

    result = poller.apply_snapshot(
        snapshot, watch_entry, prior_state, summary, now=datetime.now(UTC)
    )

    print(
        f"Notification decision: reason={result.decision.reason} "
        f"level={result.decision.notify_level or '-'}"
    )
    print(f"Email attempted: {result.decision.should_attempt_notify}")
    print(f"Email succeeded: {result.notified}")
    if result.decision.should_attempt_notify and not result.notified:
        print(
            "SMTP delivery failed - the fetched seat state was still persisted; the "
            "notification remains pending and will be retried on a later poll.",
            file=sys.stderr,
        )
    print(f"Final stored state: {_format_state(result.final_state)}")
    return 0


def _seed_closed_state(args: argparse.Namespace, section_key: SectionKey) -> int:
    """Test-only setup for poll-once-live: writes a synthetic closed prior state for
    exactly the requested section, so a later live run can demonstrate a real
    closed-to-open email transition. Makes no network call, and never touches
    course_fetch_health or the observations table - this is explicitly not a real
    observation."""
    if not args.confirm_seed:
        print(
            "Refusing to seed: pass --confirm-seed to write synthetic closed state "
            "into the database.",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(UTC)
    seeded_state = SectionState(
        section_key=section_key,
        seats_available=0,
        seats_capacity=1,
        waitlist_available=None,
        waitlist_capacity=None,
        status="closed",
        last_seen_at=now,
        last_notified_at=None,
        consecutive_failures=0,
        last_attempt_at=now,
        pending_notification_since=None,
        last_notification_attempt_at=None,
    )

    conn = connect(args.db_path)
    repository = Repository(conn)
    repository.ensure_schema()
    repository.save_state(seeded_state)

    print(
        f"Seeded synthetic closed state for {section_key.subject} "
        f"{section_key.catalog_number}-{section_key.section} ({section_key.term}) - "
        "no network call was made:"
    )
    print(f"  {_format_state(seeded_state)}")
    return 0


def _single_section_poller_config() -> PollerConfig:
    """Shared by poll-once-live and (if it ever needs a real PollerConfig object
    instead of calling diff.evaluate() directly) poll-once-dry-run.
    circuit_breaker_threshold/backoff_* are unused by Poller.apply_snapshot() itself -
    they only matter to Poller._poll_course(), which neither command calls - so their
    exact values here are inert placeholders, not a real policy."""
    return PollerConfig(
        cooldown=_SINGLE_SECTION_COOLDOWN,
        default_waitlist_policy=_SINGLE_SECTION_WAITLIST_POLICY,
        circuit_breaker_threshold=5,
        backoff_base_seconds=60,
        backoff_max_seconds=3600,
        notification_retry_delay=_SINGLE_SECTION_RETRY_DELAY,
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

    production_check_parser = subparsers.add_parser(
        "production-check",
        help=(
            "Read-only validation that everything `poll` needs is present and "
            "structurally valid: files exist, config/watches parse, db/log/lock "
            "directories are writable, SMTP settings are structurally sane. Never "
            "scrapes UNC, sends email, or touches the production database."
        ),
    )
    production_check_parser.add_argument(
        "--env-file", default=None, help="Path to .env (default: .env in the current directory)"
    )
    production_check_parser.set_defaults(func=_run_production_check)

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

    dry_run_parser = subparsers.add_parser(
        "poll-once-dry-run",
        help=(
            "Exercise the real scraper, state machine, and repository for one "
            "section, printing every step. Never sends email; writes only to the "
            "explicitly supplied --db-path."
        ),
    )
    dry_run_parser.add_argument(
        "--env-file", default=None, help="Path to .env (default: .env in the current directory)"
    )
    dry_run_parser.add_argument("--term", required=True, help='e.g. "2026 Fall"')
    dry_run_parser.add_argument("--subject", required=True, help='e.g. "COMP"')
    dry_run_parser.add_argument(
        "--catalog-number", dest="catalog_number", required=True, help='e.g. "210"'
    )
    dry_run_parser.add_argument("--section", required=True, help='e.g. "001"')
    dry_run_parser.add_argument(
        "--db-path",
        dest="db_path",
        required=True,
        help=(
            "Required - refuses to run without this. Never falls back to the "
            "production database path."
        ),
    )
    dry_run_parser.set_defaults(func=_run_poll_once_dry_run)

    test_email_parser = subparsers.add_parser(
        "test-email",
        help=(
            "Send one clearly-labeled test email via the real SmtpNotifier, to "
            "verify SMTP configuration. Never scrapes UNC, reads watches.yaml, or "
            "touches SQLite."
        ),
    )
    test_email_parser.add_argument(
        "--env-file", default=None, help="Path to .env (default: .env in the current directory)"
    )
    test_email_parser.set_defaults(func=_run_test_email)

    live_parser = subparsers.add_parser(
        "poll-once-live",
        help=(
            "Run the real end-to-end poll for one section: real scraper, real "
            "Repository, real SmtpNotifier. Requires --db-path and "
            "--confirm-send-email (or --seed-closed-state --confirm-seed for "
            "network-free test setup). Never uses the production database or "
            "watches.yaml."
        ),
    )
    live_parser.add_argument(
        "--env-file", default=None, help="Path to .env (default: .env in the current directory)"
    )
    live_parser.add_argument("--term", required=True, help='e.g. "2026 Fall"')
    live_parser.add_argument("--subject", required=True, help='e.g. "COMP"')
    live_parser.add_argument(
        "--catalog-number", dest="catalog_number", required=True, help='e.g. "210"'
    )
    live_parser.add_argument("--section", required=True, help='e.g. "001"')
    live_parser.add_argument(
        "--db-path",
        dest="db_path",
        required=True,
        help=(
            "Required - refuses to run without this. Never falls back to the "
            "production database path."
        ),
    )
    live_parser.add_argument(
        "--confirm-send-email",
        action="store_true",
        help=(
            "Required to actually run the live poll (it can send a real email via "
            "SmtpNotifier). Without it, the command refuses to run."
        ),
    )
    live_parser.add_argument(
        "--seed-closed-state",
        action="store_true",
        help=(
            "Test-only: instead of polling, write a synthetic closed prior state "
            "for this section into --db-path, then exit. Makes no network call."
        ),
    )
    live_parser.add_argument(
        "--confirm-seed",
        action="store_true",
        help="Required alongside --seed-closed-state to actually write the seeded state.",
    )
    live_parser.set_defaults(func=_run_poll_once_live)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
