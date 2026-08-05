"""CLI wiring tests. Never exercises the real UNC scraper (fetch_course always raises
NotImplementedError) - these only prove config/logging/db/notifier plumbing works."""

from __future__ import annotations

from unc_notifier.cli import main

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


def test_poll_with_real_watch_surfaces_not_implemented_scraper_as_a_failure(tmp_path, capsys):
    """The real scraper is intentionally unimplemented; a course that needs fetching
    must fail loudly (recorded as a fetch failure) rather than crash the whole run."""
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
