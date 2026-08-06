"""Real SMTP notifier. Sends one plain-text email per notification event."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from unc_notifier.models import NotificationEvent
from unc_notifier.notifier.base import Notifier


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    from_addr: str
    to_addr: str


class SmtpNotifier(Notifier):
    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def notify(self, event: NotificationEvent) -> None:
        self._send(self._build_message(event))

    def send_test_email(self) -> None:
        """Sends exactly one clearly-labeled configuration-test email - never a real
        seat alert. Used by the `test-email` CLI command to verify SMTP settings."""
        message = EmailMessage()
        message["Subject"] = "[UNC Seat Notifier] Test Email"
        message["From"] = self._config.from_addr
        message["To"] = self._config.to_addr
        message.set_content(
            "This is a configuration test email from unc-seat-notifier.\n\n"
            "It confirms your SMTP settings are working correctly. This is NOT a "
            "real seat availability alert - no course was checked and no seat has "
            "opened.\n"
        )
        self._send(message)

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._config.host, self._config.port, timeout=30) as client:
            if self._config.use_tls:
                client.starttls()
            if self._config.username and self._config.password:
                client.login(self._config.username, self._config.password)
            client.send_message(message)

    def _build_message(self, event: NotificationEvent) -> EmailMessage:
        key = event.section_key
        course = f"{key.subject} {key.catalog_number}-{key.section} ({key.term})"
        subject_prefix = "[waitlist warning] " if event.level == "warning" else ""
        message = EmailMessage()
        message["Subject"] = f"{subject_prefix}Seat open: {course}"
        message["From"] = self._config.from_addr
        message["To"] = self._config.to_addr
        message.set_content(
            f"{course} now has {event.seats_available}/{event.seats_capacity} seat(s) "
            f"available.\n\n"
            f"Status: {event.status}\n"
            f"Waitlist: {self._waitlist_summary(event)}\n"
            f"Reason: {event.reason}\n"
            f"Observed at: {event.occurred_at.isoformat()}\n"
        )
        return message

    @staticmethod
    def _waitlist_summary(event: NotificationEvent) -> str:
        # Confirmed unavailable from UNC's real search results - never guess this is
        # "0 people waiting" just because the fields are absent.
        if event.waitlist_available is None or event.waitlist_capacity is None:
            return "unknown"
        occupied = event.waitlist_capacity - event.waitlist_available
        return f"{occupied}/{event.waitlist_capacity} occupied"
