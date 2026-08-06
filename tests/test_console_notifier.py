from __future__ import annotations

from unc_notifier.models import NotificationEvent
from unc_notifier.notifier.console import ConsoleNotifier

from .helpers import SECTION, dt


def make_event(**overrides) -> NotificationEvent:
    defaults = dict(
        section_key=SECTION,
        seats_available=3,
        seats_capacity=20,
        waitlist_available=10,
        waitlist_capacity=10,
        status="open",
        reason="transition_available",
        level="info",
        occurred_at=dt(0),
    )
    defaults.update(overrides)
    return NotificationEvent(**defaults)


def test_notify_prints_section_and_seat_details(capsys):
    ConsoleNotifier().notify(make_event())

    out = capsys.readouterr().out
    assert "COMP 550-001" in out
    assert "3/20 seats" in out
    assert "reason=transition_available" in out
    assert "level=info" in out


def test_notify_prints_unknown_waitlist_honestly(capsys):
    ConsoleNotifier().notify(make_event(waitlist_available=None, waitlist_capacity=None))

    out = capsys.readouterr().out
    assert "waitlist=unknown" in out


def test_notify_prints_occupied_waitlist_when_known(capsys):
    ConsoleNotifier().notify(make_event(waitlist_available=2, waitlist_capacity=10))

    out = capsys.readouterr().out
    assert "waitlist=8/10 occupied" in out
