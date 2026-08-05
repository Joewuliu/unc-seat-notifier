"""Domain types shared across the whole system.

Nothing in this module touches the network, SQLite, SMTP, or environment
variables — it is pure data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WaitlistPolicy(StrEnum):
    """How to handle a seat opening on a section with people on the waitlist."""

    ALERT = "alert"
    WARN = "warn"
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class CourseKey:
    """Identifies one fetch: everything the scraper needs to request a course's sections."""

    term: str
    subject: str
    catalog_number: str


@dataclass(frozen=True, slots=True)
class SectionKey:
    """Identifies one watched, notifiable unit: a specific section of a course."""

    term: str
    subject: str
    catalog_number: str
    section: str

    @property
    def course_key(self) -> CourseKey:
        return CourseKey(self.term, self.subject, self.catalog_number)


@dataclass(frozen=True, slots=True)
class WatchEntry:
    """One line from watches.yaml.

    ``section`` is optional: omitted means "watch every section returned for this
    course," rather than one specific section.
    """

    term: str
    subject: str
    catalog_number: str
    section: str | None = None
    waitlist_policy: WaitlistPolicy | None = None

    @property
    def is_course_wide(self) -> bool:
        return self.section is None

    @property
    def section_key(self) -> SectionKey:
        """Only valid for a section-specific entry - see ``is_course_wide``."""
        if self.section is None:
            raise ValueError("WatchEntry.section_key requires an explicit section")
        return SectionKey(self.term, self.subject, self.catalog_number, self.section)

    @property
    def course_key(self) -> CourseKey:
        return CourseKey(self.term, self.subject, self.catalog_number)


@dataclass(frozen=True, slots=True)
class SeatSnapshot:
    """A single section's data from one successful, validated fetch."""

    section_key: SectionKey
    seats_available: int
    seats_capacity: int
    waitlist_available: int
    waitlist_capacity: int
    status: str
    observed_at: datetime

    @property
    def people_on_waitlist(self) -> int:
        return max(self.waitlist_capacity - self.waitlist_available, 0)

    @property
    def waitlist_active(self) -> bool:
        return self.people_on_waitlist > 0

    @property
    def is_available(self) -> bool:
        return self.seats_available > 0


@dataclass(frozen=True, slots=True)
class CourseFetchHealth:
    """Fetch-orchestration bookkeeping for one (term, subject, catalog_number).

    Purely about "can we successfully talk to the endpoint for this course" - never
    seat/waitlist data, and never a stand-in for a section. Exists specifically so a
    course-wide watch (no section pinned) can still have its fetch failures counted and
    its circuit breaker tripped before any real section has ever been observed.
    """

    course_key: CourseKey
    consecutive_failures: int
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class SectionState:
    """Current-state row: what we last knew about a section, plus notifier bookkeeping.

    ``pending_notification_since`` and ``last_notification_attempt_at`` model an
    outstanding delivery explicitly: a transition into availability was detected but
    the last attempt to send it (if any) hasn't succeeded yet. Both are ``None``
    whenever there's nothing pending - either nothing has ever needed sending, or the
    last send succeeded, or the section went back to unavailable before a retry could
    succeed. ``last_notified_at`` only ever advances on a *successful* send.
    """

    section_key: SectionKey
    seats_available: int
    seats_capacity: int
    waitlist_available: int
    waitlist_capacity: int
    status: str
    last_seen_at: datetime | None
    last_notified_at: datetime | None
    consecutive_failures: int
    last_attempt_at: datetime | None
    pending_notification_since: datetime | None
    last_notification_attempt_at: datetime | None

    @property
    def is_available(self) -> bool:
        return self.seats_available > 0


@dataclass(frozen=True, slots=True)
class Decision:
    """The pure output of the state machine for one section on one poll.

    ``should_attempt_notify`` means exactly that - an attempt should be made. It says
    nothing about delivery: whether that attempt succeeds is resolved by the poller
    after calling the Notifier, which then decides the section's final persisted state.
    """

    new_state: SectionState
    should_attempt_notify: bool
    reason: str
    notify_level: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """What actually gets handed to a Notifier implementation."""

    section_key: SectionKey
    seats_available: int
    seats_capacity: int
    waitlist_available: int
    waitlist_capacity: int
    status: str
    reason: str
    level: str
    occurred_at: datetime
