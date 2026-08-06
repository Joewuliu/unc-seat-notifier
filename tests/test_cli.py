"""CLI wiring tests. Never exercise the real UncClassSearchScraper - it now makes real
HTTP calls, so `poll` tests that need a course to actually be fetched monkeypatch it
with a fake, and the scrape-once tests below do the same. These only prove
config/logging/db/notifier/scraper plumbing works, not real network behavior."""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unc_notifier.cli import main
from unc_notifier.models import SeatSnapshot, SectionKey
from unc_notifier.scraper.base import FetchError
from unc_notifier.state.repository import Repository, connect

MINIMAL_ENV = """
CONTACT_EMAIL=me@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_FROM=bot@example.com
SMTP_TO=me@example.com
"""


def write_project(tmp_path, *, watches_yaml: str):
    env_path = tmp_path / ".env"
    db_path = tmp_path / "data" / "unc_notifier.db"
    log_path = tmp_path / "logs" / "unc_notifier.log"
    watches_path = tmp_path / "watches.yaml"

    env_path.write_text(
        MINIMAL_ENV
        + f"\nDB_PATH={db_path}\nLOG_PATH={log_path}\nWATCHES_PATH={watches_path}\n"
    )
    watches_path.write_text(watches_yaml)
    return env_path, db_path, log_path


def write_env(tmp_path, *, extra: str = "") -> Path:
    """A scraper-only .env: CONTACT_EMAIL alone, no SMTP or database settings. Used by
    the scrape-once tests below, which must work without any of that configured."""
    env_path = tmp_path / ".env"
    env_path.write_text("CONTACT_EMAIL=me@example.com\n" + extra)
    return env_path


class FakeFailingScraper:
    """Stands in for UncClassSearchScraper in tests - never touches the network."""

    def __init__(self, *, contact_email: str) -> None:
        self.contact_email = contact_email

    def fetch_course(self, course_key):
        raise FetchError("simulated failure - real scraper not exercised in tests")


class FakeSuccessScraper:
    """Stands in for UncClassSearchScraper, returning canned sections."""

    def __init__(self, *, contact_email: str) -> None:
        self.contact_email = contact_email

    def fetch_course(self, course_key):
        return [
            SeatSnapshot(
                section_key=SectionKey(
                    course_key.term, course_key.subject, course_key.catalog_number, "001"
                ),
                seats_available=3,
                seats_capacity=20,
                waitlist_available=None,
                waitlist_capacity=None,
                status="open",
                observed_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            )
        ]


class FakeEmptyScraper:
    """Stands in for UncClassSearchScraper on a legitimate zero-results course."""

    def __init__(self, *, contact_email: str) -> None:
        self.contact_email = contact_email

    def fetch_course(self, course_key):
        return []


def test_poll_with_no_watches_succeeds_without_touching_real_scraper(tmp_path, capsys):
    env_path, db_path, log_path = write_project(tmp_path, watches_yaml="watches: []\n")

    exit_code = main(["poll", "--env-file", str(env_path), "--skip-jitter"])

    assert exit_code == 0
    assert db_path.exists()
    assert log_path.exists()
    out = capsys.readouterr().out
    assert "courses_fetched=0" in out
    assert "sections_notified=0" in out


def test_poll_missing_env_file_fails_fast_with_exit_code_1(tmp_path, capsys):
    missing_env = tmp_path / "does-not-exist.env"

    exit_code = main(["poll", "--env-file", str(missing_env), "--skip-jitter"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Configuration error" in err


def test_poll_with_scraper_failure_records_it_without_crashing_the_run(
    tmp_path, capsys, monkeypatch
):
    """A course fetch failure must be recorded loudly (as a fetch failure) rather than
    crash the whole run. Uses FakeFailingScraper in place of UncClassSearchScraper so
    this never touches the real network."""
    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", FakeFailingScraper)
    env_path, _, _ = write_project(
        tmp_path,
        watches_yaml=(
            "watches:\n  - term: '2026 Spring'\n    subject: 'COMP'\n"
            "    catalog_number: '550'\n    section: '001'\n"
        ),
    )

    exit_code = main(["poll", "--env-file", str(env_path), "--skip-jitter"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "courses_failed=1" in out


# --- scrape-once -----------------------------------------------------------------------


def _scrape_once_args(env_path) -> list[str]:
    return [
        "scrape-once",
        "--env-file", str(env_path),
        "--term", "2026 Fall",
        "--subject", "COMP",
        "--catalog-number", "210",
    ]


def test_scrape_once_prints_readable_output_on_success(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", FakeSuccessScraper)
    env_path = write_env(tmp_path)

    exit_code = main(_scrape_once_args(env_path))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "COMP 210 (2026 Fall)" in out
    assert "section 001" in out
    assert "3/20 seats available" in out
    assert "status=open" in out
    assert "waitlist=unknown" in out


def test_scrape_once_zero_results_exits_successfully(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", FakeEmptyScraper)
    env_path = write_env(tmp_path)

    exit_code = main(_scrape_once_args(env_path))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No sections found." in out


def test_scrape_once_fetch_error_exits_nonzero_with_clear_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", FakeFailingScraper)
    env_path = write_env(tmp_path)

    exit_code = main(_scrape_once_args(env_path))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Fetch failed" in err
    assert "simulated failure" in err


def test_scrape_once_succeeds_with_invalid_or_missing_smtp_values(tmp_path, capsys, monkeypatch):
    """scrape-once must not read, validate, or require SMTP settings at all - proven
    here with SMTP_PORT set to garbage and every other SMTP field left unset, which
    would fail the poll command's config loader."""
    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", FakeSuccessScraper)
    env_path = write_env(tmp_path, extra="SMTP_PORT=not-a-number\n")

    exit_code = main(_scrape_once_args(env_path))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "3/20 seats available" in out


def test_scrape_once_missing_contact_email_gives_clear_error(tmp_path, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_OTHER_VAR=1\n")

    exit_code = main(_scrape_once_args(env_path))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "CONTACT_EMAIL" in err


def test_scrape_once_missing_env_file_fails_fast_with_exit_code_1(tmp_path, capsys):
    missing_env = tmp_path / "does-not-exist.env"

    exit_code = main(_scrape_once_args(missing_env))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Configuration error" in err


def test_scrape_once_never_constructs_repository_or_notifier(tmp_path, capsys, monkeypatch):
    """The whole point of scrape-once is that it cannot send email or write state -
    prove Repository/SmtpNotifier/connect are never even constructed, not just unused."""

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("must not be called by scrape-once")

    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", FakeSuccessScraper)
    monkeypatch.setattr("unc_notifier.cli.Repository", _must_not_be_called)
    monkeypatch.setattr("unc_notifier.cli.SmtpNotifier", _must_not_be_called)
    monkeypatch.setattr("unc_notifier.cli.connect", _must_not_be_called)
    env_path = write_env(tmp_path)

    exit_code = main(_scrape_once_args(env_path))

    assert exit_code == 0


# --- poll-once-dry-run -------------------------------------------------------------

DRY_RUN_SECTION = SectionKey("2026 Fall", "COMP", "210", "001")


def _dry_run_args(env_path, db_path) -> list[str]:
    return [
        "poll-once-dry-run",
        "--env-file", str(env_path),
        "--term", "2026 Fall",
        "--subject", "COMP",
        "--catalog-number", "210",
        "--section", "001",
        "--db-path", str(db_path),
    ]


def dry_run_snapshot(seats_available: int, *, observed_at: datetime) -> SeatSnapshot:
    return SeatSnapshot(
        section_key=DRY_RUN_SECTION,
        seats_available=seats_available,
        seats_capacity=150,
        waitlist_available=None,
        waitlist_capacity=None,
        status="open" if seats_available > 0 else "closed",
        observed_at=observed_at,
    )


def dry_run_time(hour: int) -> datetime:
    return datetime(2026, 1, 15, hour, 0, tzinfo=UTC)


def make_scripted_scraper(responses):
    """Returns a class standing in for UncClassSearchScraper whose fetch_course()
    yields the next scripted response (a list[SeatSnapshot], or an Exception instance
    to raise) on each call - across however many separate `main([...])` invocations
    use it, since poll-once-dry-run constructs a fresh scraper instance every run.
    Never touches the network."""
    queue = list(responses)

    class _ScriptedScraper:
        def __init__(self, *, contact_email: str) -> None:
            self.contact_email = contact_email

        def fetch_course(self, course_key):
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    return _ScriptedScraper


def test_poll_once_dry_run_never_constructs_smtp_notifier(tmp_path, monkeypatch):
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("must not be called by poll-once-dry-run")

    monkeypatch.setattr(
        "unc_notifier.cli.UncClassSearchScraper",
        make_scripted_scraper([[dry_run_snapshot(0, observed_at=dry_run_time(12))]]),
    )
    monkeypatch.setattr("unc_notifier.cli.SmtpNotifier", _must_not_be_called)
    env_path = write_env(tmp_path)
    db_path = tmp_path / "dry-run.db"

    exit_code = main(_dry_run_args(env_path, db_path))

    assert exit_code == 0


def test_poll_once_dry_run_requires_db_path(tmp_path):
    env_path = write_env(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "poll-once-dry-run",
                "--env-file", str(env_path),
                "--term", "2026 Fall",
                "--subject", "COMP",
                "--catalog-number", "210",
                "--section", "001",
            ]
        )

    assert exc_info.value.code != 0


def test_poll_once_dry_run_uses_the_supplied_db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "unc_notifier.cli.UncClassSearchScraper",
        make_scripted_scraper([[dry_run_snapshot(3, observed_at=dry_run_time(12))]]),
    )
    env_path = write_env(tmp_path)
    db_path = tmp_path / "custom" / "dry-run.db"
    default_looking_path = tmp_path / "data" / "unc_notifier.db"

    exit_code = main(_dry_run_args(env_path, db_path))

    assert exit_code == 0
    assert db_path.exists()
    assert not default_looking_path.exists()

    state = Repository(connect(db_path)).get_section_state(DRY_RUN_SECTION)
    assert state is not None
    assert state.seats_available == 3


def test_poll_once_dry_run_persists_first_observation_without_notifying(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        "unc_notifier.cli.UncClassSearchScraper",
        make_scripted_scraper([[dry_run_snapshot(3, observed_at=dry_run_time(12))]]),
    )
    env_path = write_env(tmp_path)
    db_path = tmp_path / "dry-run.db"

    exit_code = main(_dry_run_args(env_path, db_path))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Previous stored state: none" in out
    assert "Detected transition: first observation" in out
    assert "reason=first_observation" in out
    assert "Would notify: False" in out
    assert "[notification]" not in out

    state = Repository(connect(db_path)).get_section_state(DRY_RUN_SECTION)
    assert state is not None
    assert state.seats_available == 3
    assert state.last_notified_at is None


def test_poll_once_dry_run_closed_to_open_transition_notifies_via_console(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        "unc_notifier.cli.UncClassSearchScraper",
        make_scripted_scraper(
            [
                [dry_run_snapshot(0, observed_at=dry_run_time(12))],
                [dry_run_snapshot(5, observed_at=dry_run_time(13))],
            ]
        ),
    )
    env_path = write_env(tmp_path)
    db_path = tmp_path / "dry-run.db"

    main(_dry_run_args(env_path, db_path))  # run 1: closed, first observation
    capsys.readouterr()  # discard run 1's output

    exit_code = main(_dry_run_args(env_path, db_path))  # run 2: closed -> open

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Detected transition: unavailable -> available" in out
    assert "reason=transition_available" in out
    assert "Would notify: True" in out
    assert "[notification] COMP 210-001" in out

    state = Repository(connect(db_path)).get_section_state(DRY_RUN_SECTION)
    assert state.last_notified_at is not None


def test_poll_once_dry_run_repeated_unchanged_observation_does_not_duplicate_alert(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        "unc_notifier.cli.UncClassSearchScraper",
        make_scripted_scraper(
            [
                [dry_run_snapshot(0, observed_at=dry_run_time(12))],
                [dry_run_snapshot(5, observed_at=dry_run_time(13))],
                [dry_run_snapshot(5, observed_at=dry_run_time(14))],
            ]
        ),
    )
    env_path = write_env(tmp_path)
    db_path = tmp_path / "dry-run.db"

    main(_dry_run_args(env_path, db_path))  # run 1: closed, first observation
    main(_dry_run_args(env_path, db_path))  # run 2: closed -> open, notifies
    capsys.readouterr()  # discard runs 1-2 output

    exit_code = main(_dry_run_args(env_path, db_path))  # run 3: still open, unchanged

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Detected transition: no change (available)" in out
    assert "Would notify: False" in out
    assert "[notification]" not in out


def test_poll_once_dry_run_fetch_error_does_not_corrupt_prior_state(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        "unc_notifier.cli.UncClassSearchScraper",
        make_scripted_scraper(
            [
                [dry_run_snapshot(3, observed_at=dry_run_time(12))],
                FetchError("simulated outage"),
            ]
        ),
    )
    env_path = write_env(tmp_path)
    db_path = tmp_path / "dry-run.db"

    main(_dry_run_args(env_path, db_path))  # run 1: succeeds, persists real state
    capsys.readouterr()  # discard run 1's output

    exit_code = main(_dry_run_args(env_path, db_path))  # run 2: fetch fails

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Fetch failed" in err
    assert "simulated outage" in err

    state = Repository(connect(db_path)).get_section_state(DRY_RUN_SECTION)
    assert state is not None
    assert state.seats_available == 3  # unchanged from run 1
    assert state.last_seen_at is not None


# --- test-email ----------------------------------------------------------------------

SMTP_ONLY_ENV = (
    "SMTP_HOST=smtp.example.com\n"
    "SMTP_PORT=587\n"
    "SMTP_FROM=bot@example.com\n"
    "SMTP_TO=me@example.com\n"
    "SMTP_USERNAME=smtpuser\n"
    "SMTP_PASSWORD=super-secret-password\n"
)


def write_smtp_env(tmp_path, *, extra: str = "") -> Path:
    """An SMTP-only .env - no CONTACT_EMAIL, no database settings. Used by the
    test-email tests below, which must work without any of that configured."""
    env_path = tmp_path / ".env"
    env_path.write_text(SMTP_ONLY_ENV + extra)
    return env_path


def _mock_smtp(monkeypatch) -> MagicMock:
    """Patches smtplib.SMTP so SmtpNotifier really runs (real message, real
    connect/tls/login/send_message call sequence) without touching a real socket."""
    mock_smtp_cls = MagicMock()
    monkeypatch.setattr("unc_notifier.notifier.smtp.smtplib.SMTP", mock_smtp_cls)
    return mock_smtp_cls.return_value.__enter__.return_value


def test_test_email_sends_clearly_labeled_message_on_success(tmp_path, capsys, monkeypatch):
    client = _mock_smtp(monkeypatch)
    env_path = write_smtp_env(tmp_path)

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 0
    client.send_message.assert_called_once()
    sent = client.send_message.call_args[0][0]
    assert sent["Subject"] == "[UNC Seat Notifier] Test Email"
    body = sent.get_content().lower()
    assert "configuration test" in body
    assert "not a real" in body

    out = capsys.readouterr().out
    assert "Test email sent successfully." in out


def test_test_email_smtp_failure_exits_nonzero_with_clear_message(tmp_path, capsys, monkeypatch):
    client = _mock_smtp(monkeypatch)
    client.send_message.side_effect = smtplib.SMTPException("relay refused")
    env_path = write_smtp_env(tmp_path)

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "SMTP test email failed" in err
    assert "relay refused" in err
    out = capsys.readouterr().out
    assert "sent successfully" not in out


def test_test_email_missing_configuration_exits_nonzero(tmp_path, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_OTHER_VAR=1\n")

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "SMTP_HOST" in err


def test_test_email_never_prints_smtp_password_on_success(tmp_path, capsys, monkeypatch):
    _mock_smtp(monkeypatch)
    env_path = write_smtp_env(tmp_path)

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "super-secret-password" not in out
    assert "smtpuser" not in out


def test_test_email_redacts_password_from_error_output(tmp_path, capsys, monkeypatch):
    """Even if the SMTP error message itself happened to echo the password (e.g. a
    misconfigured relay bouncing back what it received), it must never reach stderr
    unredacted."""
    client = _mock_smtp(monkeypatch)
    client.send_message.side_effect = smtplib.SMTPAuthenticationError(
        535, b"auth failed for password super-secret-password"
    )
    env_path = write_smtp_env(tmp_path)

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "super-secret-password" not in err
    assert "REDACTED" in err


def test_test_email_never_constructs_scraper_repository_poller_or_sqlite(
    tmp_path, monkeypatch
):
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("must not be called by test-email")

    _mock_smtp(monkeypatch)
    monkeypatch.setattr("unc_notifier.cli.UncClassSearchScraper", _must_not_be_called)
    monkeypatch.setattr("unc_notifier.cli.Repository", _must_not_be_called)
    monkeypatch.setattr("unc_notifier.cli.Poller", _must_not_be_called)
    monkeypatch.setattr("unc_notifier.cli.connect", _must_not_be_called)
    env_path = write_smtp_env(tmp_path)

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 0


def test_test_email_does_not_read_watches_yaml(tmp_path, monkeypatch):
    """No watches.yaml exists at all in this tmp_path - if test-email tried to read
    one, it would fail with a ConfigError, not succeed."""
    _mock_smtp(monkeypatch)
    env_path = write_smtp_env(tmp_path)
    assert not (tmp_path / "watches.yaml").exists()

    exit_code = main(["test-email", "--env-file", str(env_path)])

    assert exit_code == 0
