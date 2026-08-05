"""In-memory notifier for tests: records events instead of sending email.

Can be scripted to fail its first N calls (simulating an SMTP outage) before
succeeding, so poller-level retry behavior is testable without touching smtplib.
"""

from __future__ import annotations

from unc_notifier.models import NotificationEvent
from unc_notifier.notifier.base import Notifier


class FakeNotifier(Notifier):
    def __init__(self, *, fail_times: int = 0, exception: Exception | None = None) -> None:
        self.events: list[NotificationEvent] = []
        self.attempts: list[NotificationEvent] = []
        self._fail_times = fail_times
        self._exception = exception or RuntimeError("simulated SMTP failure")

    def notify(self, event: NotificationEvent) -> None:
        self.attempts.append(event)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._exception
        self.events.append(event)
