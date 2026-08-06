"""Prints notification events to stdout instead of sending email.

Used by the `poll-once-dry-run` CLI command so the real notification-decision logic
(diff.evaluate + Repository) can be exercised end-to-end without ever contacting an
SMTP server.
"""

from __future__ import annotations

from unc_notifier.models import NotificationEvent
from unc_notifier.notifier.base import Notifier


class ConsoleNotifier(Notifier):
    def notify(self, event: NotificationEvent) -> None:
        key = event.section_key
        if event.waitlist_available is None or event.waitlist_capacity is None:
            waitlist = "unknown"
        else:
            occupied = event.waitlist_capacity - event.waitlist_available
            waitlist = f"{occupied}/{event.waitlist_capacity} occupied"
        print(
            f"[notification] {key.subject} {key.catalog_number}-{key.section} "
            f"({key.term}): {event.seats_available}/{event.seats_capacity} seats "
            f"available (status={event.status}, waitlist={waitlist}) - "
            f"reason={event.reason} level={event.level}"
        )
