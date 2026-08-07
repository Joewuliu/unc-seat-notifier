"""Loads .env and watches.yaml into typed config objects.

The only module that touches environment variables or the config file's disk path.
Everything else in the system receives already-parsed dataclasses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import dotenv_values

from unc_notifier.models import WaitlistPolicy, WatchEntry
from unc_notifier.notifier.smtp import SmtpConfig


class ConfigError(ValueError):
    """Raised for missing or invalid configuration. Meant to fail fast and loudly,
    never to fall back to a silent default for anything security- or correctness-
    sensitive (credentials, recipient addresses, required identifiers)."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    contact_email: str
    smtp: SmtpConfig
    db_path: Path
    log_path: Path
    watches_path: Path
    lock_path: Path
    poll_interval_minutes: int
    jitter_seconds: int
    cooldown_minutes: int
    circuit_breaker_threshold: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    default_waitlist_policy: WaitlistPolicy
    notification_retry_delay_minutes: int


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    """Everything UncClassSearchScraper actually needs - nothing else. Deliberately
    separate from AppConfig so a scraper-only command (scrape-once) never reads,
    validates, or requires SMTP/database settings it has no use for."""

    contact_email: str


_REQUIRED = ("CONTACT_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_TO")
_SMTP_REQUIRED = ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_TO")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _load_env_values(env_path: str | Path | None) -> dict[str, str]:
    """.env (defaulting to the current directory's .env) merged with the process
    environment, which takes precedence."""
    file_values = dotenv_values(env_path) if env_path is not None else dotenv_values()
    return {**file_values, **os.environ}


def _path_base_dir(env_path: str | Path | None) -> Path:
    """Where a relative DB_PATH/LOG_PATH/WATCHES_PATH/LOCK_PATH should be resolved
    against. When --env-file is given explicitly (as the production launcher always
    does, using an absolute path computed from its own script location), relative
    paths resolve against that .env file's directory - never the process's current
    working directory, so a scheduler launching from an arbitrary "start in" folder
    can't silently point at the wrong files. Falls back to the current working
    directory only for the interactive/dev case of no --env-file at all, matching
    dotenv's own default CWD-based .env discovery."""
    if env_path is None:
        return Path.cwd()
    return Path(env_path).resolve().parent


def _resolve_configured_path(raw: str, *, base_dir: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base_dir / path


def load_scraper_config(env_path: str | Path | None = None) -> ScraperConfig:
    """Read only CONTACT_EMAIL from .env/environment. Used by scrape-once, which must
    be runnable without any SMTP or database configuration - see load_config() for the
    full poll-command configuration, which this deliberately does not call."""
    values = _load_env_values(env_path)

    contact_email = values.get("CONTACT_EMAIL")
    if not contact_email:
        raise ConfigError("Missing required .env value: CONTACT_EMAIL")

    return ScraperConfig(contact_email=contact_email)


def load_smtp_config(env_path: str | Path | None = None) -> SmtpConfig:
    """Read only the SMTP_* values from .env/environment. Used by test-email, which
    must be runnable without CONTACT_EMAIL, watches.yaml, or any database
    configuration - see load_config() for the full poll-command configuration, which
    this deliberately does not call."""
    values = _load_env_values(env_path)

    missing = [key for key in _SMTP_REQUIRED if not values.get(key)]
    if missing:
        raise ConfigError(f"Missing required .env value(s): {', '.join(missing)}")

    try:
        smtp_port = int(values["SMTP_PORT"])
    except ValueError as exc:
        raise ConfigError(f"SMTP_PORT must be an integer, got {values['SMTP_PORT']!r}") from exc

    return SmtpConfig(
        host=values["SMTP_HOST"],
        port=smtp_port,
        username=values.get("SMTP_USERNAME") or None,
        password=values.get("SMTP_PASSWORD") or None,
        use_tls=_bool(values.get("SMTP_USE_TLS", "true")),
        from_addr=values["SMTP_FROM"],
        to_addr=values["SMTP_TO"],
    )


def load_config(env_path: str | Path | None = None) -> AppConfig:
    """Read .env (defaulting to the current directory's .env) plus process environment,
    with process environment taking precedence. Raises ConfigError if anything required
    is missing or malformed."""
    values = _load_env_values(env_path)

    missing = [key for key in _REQUIRED if not values.get(key)]
    if missing:
        raise ConfigError(f"Missing required .env value(s): {', '.join(missing)}")

    try:
        smtp_port = int(values["SMTP_PORT"])
    except ValueError as exc:
        raise ConfigError(f"SMTP_PORT must be an integer, got {values['SMTP_PORT']!r}") from exc

    policy_raw = values.get("DEFAULT_WAITLIST_POLICY", "alert")
    try:
        default_policy = WaitlistPolicy(policy_raw.lower())
    except ValueError as exc:
        valid = ", ".join(p.value for p in WaitlistPolicy)
        raise ConfigError(
            f"DEFAULT_WAITLIST_POLICY must be one of [{valid}], got {policy_raw!r}"
        ) from exc

    base_dir = _path_base_dir(env_path)
    db_path = _resolve_configured_path(
        values.get("DB_PATH", "data/unc_notifier.db"), base_dir=base_dir
    )
    default_lock_path = db_path.parent / "unc_notifier.lock"
    lock_path = (
        _resolve_configured_path(values["LOCK_PATH"], base_dir=base_dir)
        if values.get("LOCK_PATH")
        else default_lock_path
    )

    try:
        return AppConfig(
            contact_email=values["CONTACT_EMAIL"],
            smtp=SmtpConfig(
                host=values["SMTP_HOST"],
                port=smtp_port,
                username=values.get("SMTP_USERNAME") or None,
                password=values.get("SMTP_PASSWORD") or None,
                use_tls=_bool(values.get("SMTP_USE_TLS", "true")),
                from_addr=values["SMTP_FROM"],
                to_addr=values["SMTP_TO"],
            ),
            db_path=db_path,
            log_path=_resolve_configured_path(
                values.get("LOG_PATH", "logs/unc_notifier.log"), base_dir=base_dir
            ),
            watches_path=_resolve_configured_path(
                values.get("WATCHES_PATH", "watches.yaml"), base_dir=base_dir
            ),
            lock_path=lock_path,
            poll_interval_minutes=int(values.get("POLL_INTERVAL_MINUTES", "5")),
            jitter_seconds=int(values.get("JITTER_SECONDS", "30")),
            cooldown_minutes=int(values.get("COOLDOWN_MINUTES", "30")),
            circuit_breaker_threshold=int(values.get("CIRCUIT_BREAKER_THRESHOLD", "5")),
            backoff_base_seconds=float(values.get("BACKOFF_BASE_SECONDS", "60")),
            backoff_max_seconds=float(values.get("BACKOFF_MAX_SECONDS", "3600")),
            default_waitlist_policy=default_policy,
            notification_retry_delay_minutes=int(
                values.get("NOTIFICATION_RETRY_DELAY_MINUTES", "5")
            ),
        )
    except ValueError as exc:
        raise ConfigError(f"Invalid numeric .env value: {exc}") from exc


def load_watches(path: str | Path) -> list[WatchEntry]:
    """Parse watches.yaml into WatchEntry objects. Raises ConfigError on anything
    missing or malformed rather than silently skipping a bad entry."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"watches file not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("watches")
    if entries is None:
        raise ConfigError(f"{path} must have a top-level 'watches' list")

    watches: list[WatchEntry] = []
    for i, entry in enumerate(entries):
        try:
            term = entry["term"]
            subject = entry["subject"]
            catalog_number = str(entry["catalog_number"])
        except KeyError as exc:
            raise ConfigError(
                f"{path}: watches[{i}] is missing required field {exc}"
            ) from exc

        section_raw = entry.get("section")
        section = str(section_raw) if section_raw is not None else None

        policy_raw = entry.get("waitlist_policy")
        policy = None
        if policy_raw is not None:
            try:
                policy = WaitlistPolicy(str(policy_raw).lower())
            except ValueError as exc:
                valid = ", ".join(p.value for p in WaitlistPolicy)
                raise ConfigError(
                    f"{path}: watches[{i}].waitlist_policy must be one of [{valid}], "
                    f"got {policy_raw!r}"
                ) from exc

        watches.append(
            WatchEntry(
                term=term,
                subject=subject,
                catalog_number=catalog_number,
                section=section,
                waitlist_policy=policy,
            )
        )

    return watches
