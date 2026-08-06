"""CLI wiring tests. Never exercise the real UncClassSearchScraper - it now makes real
HTTP calls, so `poll` tests that need a course to actually be fetched monkeypatch it
with a fake, and the scrape-once tests below do the same. These only prove
config/logging/db/notifier/scraper plumbing works, not real network behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from unc_notifier.cli import main
from unc_notifier.models import SeatSnapshot, SectionKey
from unc_notifier.scraper.base import FetchError

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
