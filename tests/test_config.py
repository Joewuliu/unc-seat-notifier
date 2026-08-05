from __future__ import annotations

import pytest

from unc_notifier.config import ConfigError, load_config, load_watches
from unc_notifier.models import WaitlistPolicy

MINIMAL_ENV = """
CONTACT_EMAIL=me@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_FROM=bot@example.com
SMTP_TO=me@example.com
"""


def write_env(tmp_path, contents: str):
    path = tmp_path / ".env"
    path.write_text(contents)
    return path


def test_load_config_applies_defaults_for_optional_values(tmp_path):
    env_path = write_env(tmp_path, MINIMAL_ENV)

    config = load_config(env_path)

    assert config.contact_email == "me@example.com"
    assert config.smtp.host == "smtp.example.com"
    assert config.smtp.port == 587
    assert config.smtp.username is None
    assert config.cooldown_minutes == 30
    assert config.poll_interval_minutes == 5
    assert config.default_waitlist_policy == WaitlistPolicy.ALERT


def test_load_config_missing_required_value_raises_config_error(tmp_path):
    env_path = write_env(
        tmp_path,
        "CONTACT_EMAIL=me@example.com\nSMTP_HOST=smtp.example.com\n",
    )

    with pytest.raises(ConfigError, match="SMTP_PORT"):
        load_config(env_path)


def test_load_config_invalid_smtp_port_raises_config_error(tmp_path):
    env_path = write_env(tmp_path, MINIMAL_ENV.replace("SMTP_PORT=587", "SMTP_PORT=not-a-number"))

    with pytest.raises(ConfigError, match="SMTP_PORT"):
        load_config(env_path)


def test_load_config_invalid_waitlist_policy_raises_config_error(tmp_path):
    env_path = write_env(tmp_path, MINIMAL_ENV + "\nDEFAULT_WAITLIST_POLICY=yell-loudly\n")

    with pytest.raises(ConfigError, match="DEFAULT_WAITLIST_POLICY"):
        load_config(env_path)


def test_load_config_reads_custom_values(tmp_path):
    env_path = write_env(tmp_path, MINIMAL_ENV + "\nCOOLDOWN_MINUTES=45\nSMTP_USE_TLS=false\n")

    config = load_config(env_path)

    assert config.cooldown_minutes == 45
    assert config.smtp.use_tls is False


WATCHES_YAML = """
watches:
  - term: "2026 Spring"
    subject: "COMP"
    catalog_number: "550"
    section: "001"
  - term: "2026 Spring"
    subject: "MATH"
    catalog_number: "231"
    section: "001"
    waitlist_policy: "warn"
"""


def test_load_watches_parses_entries(tmp_path):
    path = tmp_path / "watches.yaml"
    path.write_text(WATCHES_YAML)

    watches = load_watches(path)

    assert len(watches) == 2
    assert watches[0].subject == "COMP"
    assert watches[0].waitlist_policy is None
    assert watches[1].waitlist_policy == WaitlistPolicy.WARN


def test_load_watches_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_watches(tmp_path / "does-not-exist.yaml")


def test_load_watches_missing_required_field_raises_config_error(tmp_path):
    path = tmp_path / "watches.yaml"
    path.write_text("watches:\n  - term: '2026 Spring'\n    subject: 'COMP'\n")

    with pytest.raises(ConfigError, match="catalog_number"):
        load_watches(path)


def test_load_watches_invalid_waitlist_policy_raises_config_error(tmp_path):
    path = tmp_path / "watches.yaml"
    path.write_text(
        "watches:\n  - term: '2026 Spring'\n    subject: 'COMP'\n"
        "    catalog_number: '550'\n    section: '001'\n    waitlist_policy: 'yell'\n"
    )

    with pytest.raises(ConfigError, match="waitlist_policy"):
        load_watches(path)
