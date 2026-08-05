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
    poll_interval_minutes: int
    jitter_seconds: int
    cooldown_minutes: int
    circuit_breaker_threshold: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    default_waitlist_policy: WaitlistPolicy
    notification_retry_delay_minutes: int


_REQUIRED = ("CONTACT_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_TO")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_config(env_path: str | Path | None = None) -> AppConfig:
    """Read .env (defaulting to the current directory's .env) plus process environment,
    with process environment taking precedence. Raises ConfigError if anything required
    is missing or malformed."""
    file_values = dotenv_values(env_path) if env_path is not None else dotenv_values()
    values = {**file_values, **os.environ}

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
            db_path=Path(values.get("DB_PATH", "data/unc_notifier.db")),
            log_path=Path(values.get("LOG_PATH", "logs/unc_notifier.log")),
            watches_path=Path(values.get("WATCHES_PATH", "watches.yaml")),
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
