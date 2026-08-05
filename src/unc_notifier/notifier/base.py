"""Abstract notifier interface, mirroring the scraper interface: the poller depends on
this, never on smtplib directly, so the state machine and orchestration are testable
without sending real email."""

from __future__ import annotations

from abc import ABC, abstractmethod

from unc_notifier.models import NotificationEvent


class Notifier(ABC):
    @abstractmethod
    def notify(self, event: NotificationEvent) -> None:
        raise NotImplementedError
