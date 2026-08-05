"""Small factories shared by tests, so each test only spells out the fields it cares about."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from unc_notifier.models import CourseKey, SeatSnapshot, SectionKey, SectionState

SECTION = SectionKey(term="2026 Spring", subject="COMP", catalog_number="550", section="001")

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture_snapshots(filename: str) -> list[SeatSnapshot]:
    """Load one of our own normalized test fixtures (NOT a guess at UNC's real schema —
    see SPEC.md) and turn it into the SeatSnapshot list a Scraper.fetch_course would
    return for that course."""
    payload = json.loads((FIXTURES_DIR / filename).read_text())
    observed_at = datetime.fromisoformat(payload["observed_at"])
    return [
        SeatSnapshot(
            section_key=SectionKey(
                term=payload["term"],
                subject=payload["subject"],
                catalog_number=payload["catalog_number"],
                section=section["section"],
            ),
            seats_available=section["seats_available"],
            seats_capacity=section["seats_capacity"],
            waitlist_available=section["waitlist_available"],
            waitlist_capacity=section["waitlist_capacity"],
            status=section["status"],
            observed_at=observed_at,
        )
        for section in payload["sections"]
    ]


def fixture_course_key(filename: str) -> CourseKey:
    payload = json.loads((FIXTURES_DIR / filename).read_text())
    return CourseKey(
        term=payload["term"], subject=payload["subject"], catalog_number=payload["catalog_number"]
    )


def dt(minute: int, hour: int = 12) -> datetime:
    """A deterministic UTC timestamp, minute-resolution, for readable test data."""
    return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)


def make_snapshot(
    *,
    section_key: SectionKey = SECTION,
    seats_available: int = 0,
    seats_capacity: int = 20,
    waitlist_available: int = 10,
    waitlist_capacity: int = 10,
    status: str = "open",
    observed_at: datetime | None = None,
) -> SeatSnapshot:
    return SeatSnapshot(
        section_key=section_key,
        seats_available=seats_available,
        seats_capacity=seats_capacity,
        waitlist_available=waitlist_available,
        waitlist_capacity=waitlist_capacity,
        status=status,
        observed_at=observed_at if observed_at is not None else dt(0),
    )


def make_state(
    *,
    section_key: SectionKey = SECTION,
    seats_available: int = 0,
    seats_capacity: int = 20,
    waitlist_available: int = 10,
    waitlist_capacity: int = 10,
    status: str = "open",
    last_seen_at: datetime | None = None,
    last_notified_at: datetime | None = None,
    consecutive_failures: int = 0,
    last_attempt_at: datetime | None = None,
    pending_notification_since: datetime | None = None,
    last_notification_attempt_at: datetime | None = None,
) -> SectionState:
    return SectionState(
        section_key=section_key,
        seats_available=seats_available,
        seats_capacity=seats_capacity,
        waitlist_available=waitlist_available,
        waitlist_capacity=waitlist_capacity,
        status=status,
        last_seen_at=last_seen_at if last_seen_at is not None else dt(0),
        last_notified_at=last_notified_at,
        consecutive_failures=consecutive_failures,
        last_attempt_at=last_attempt_at if last_attempt_at is not None else dt(0),
        pending_notification_since=pending_notification_since,
        last_notification_attempt_at=last_notification_attempt_at,
    )
