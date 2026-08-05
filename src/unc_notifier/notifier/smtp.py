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
        message = self._build_message(event)
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
            f"Waitlist: {event.waitlist_capacity - event.waitlist_available}/"
            f"{event.waitlist_capacity} occupied\n"
            f"Reason: {event.reason}\n"
            f"Observed at: {event.occurred_at.isoformat()}\n"
        )
        return message
