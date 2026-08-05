"""Poller orchestration tests: dedup, failure isolation, and circuit-breaker backoff.

Exercises the full stack (fake scraper -> poller -> in-memory repository -> fake
notifier) but never touches real SQLite-on-disk, network, or SMTP.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

import pytest

from unc_notifier.models import CourseKey, SeatSnapshot, SectionKey, WaitlistPolicy, WatchEntry
from unc_notifier.notifier.fake import FakeNotifier
from unc_notifier.poller import Poller, PollerConfig
from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.fake import FakeScraper
from unc_notifier.state.repository import Repository

from .helpers import dt

COURSE = CourseKey(term="2026 Spring", subject="COMP", catalog_number="550")
OTHER_COURSE = CourseKey(term="2026 Spring", subject="MATH", catalog_number="231")


@pytest.fixture
def repository() -> Repository:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    repo = Repository(conn)
    repo.ensure_schema()
    return repo


@pytest.fixture
def config() -> PollerConfig:
    return PollerConfig(
        cooldown=timedelta(minutes=30),
        default_waitlist_policy=WaitlistPolicy.ALERT,
        circuit_breaker_threshold=3,
        backoff_base_seconds=60,
        backoff_max_seconds=3600,
        notification_retry_delay=timedelta(minutes=5),
    )


def snapshot(section: str, seats: int, *, observed_at) -> SeatSnapshot:
    return SeatSnapshot(
        section_key=SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, section),
        seats_available=seats,
        seats_capacity=20,
        waitlist_available=10,
        waitlist_capacity=10,
        status="open" if seats else "closed",
        observed_at=observed_at,
    )


def test_two_watches_same_course_dedupe_to_one_fetch(repository, config):
    watches = [
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001"),
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "002"),
    ]
    scraper = FakeScraper(
        {
            COURSE: [
                snapshot("001", 3, observed_at=dt(0)),
                snapshot("002", 0, observed_at=dt(0)),
            ]
        }
    )
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert scraper.calls == [COURSE]
    assert summary.courses_fetched == 1
    assert summary.sections_observed == 2
    section_001 = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    section_002 = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "002")
    assert repository.get_section_state(section_001).seats_available == 3
    assert repository.get_section_state(section_002).seats_available == 0
    # First-ever observation for both -> no notification yet.
    assert notifier.events == []


def test_one_course_failure_does_not_block_other_courses(repository, config):
    watches = [
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001"),
        WatchEntry(OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number, "001"),
    ]
    scraper = FakeScraper(
        {
            COURSE: FetchError("simulated outage"),
            OTHER_COURSE: [
                SeatSnapshot(
                    section_key=SectionKey(
                        OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number,
                        "001",
                    ),
                    seats_available=2,
                    seats_capacity=30,
                    waitlist_available=5,
                    waitlist_capacity=5,
                    status="open",
                    observed_at=dt(0),
                )
            ],
        }
    )
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert summary.courses_failed == 1
    assert summary.courses_fetched == 1

    failing_section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    healthy_section = SectionKey(
        OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number, "001"
    )
    failing_state = repository.get_section_state(failing_section)
    assert failing_state.consecutive_failures == 1
    assert failing_state.last_seen_at is None  # never corrupted with fake "0 seats"

    healthy_state = repository.get_section_state(healthy_section)
    assert healthy_state.seats_available == 2
    assert healthy_state.consecutive_failures == 0


def test_repeated_failures_open_circuit_and_skip_future_fetches_until_backoff_elapses(
    repository, config
):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    scraper = FakeScraper({COURSE: FetchError("down")})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    # Three consecutive failures trips the breaker (threshold=3).
    poller.run(watches, now=dt(0))
    poller.run(watches, now=dt(1))
    summary3 = poller.run(watches, now=dt(2))
    assert summary3.courses_failed == 1
    assert scraper.calls == [COURSE, COURSE, COURSE]

    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    assert repository.get_section_state(section).consecutive_failures == 3

    # Immediately after: still within the 60s backoff window, so the course is skipped.
    summary4 = poller.run(watches, now=dt(2) + timedelta(seconds=30))
    assert summary4.courses_skipped_circuit_open == 1
    assert scraper.calls == [COURSE, COURSE, COURSE]  # unchanged

    # After the backoff window elapses, the course is attempted again.
    later = dt(2) + timedelta(seconds=61)
    summary5 = poller.run(watches, now=later)
    assert summary5.courses_skipped_circuit_open == 0
    assert summary5.courses_failed == 1
    assert scraper.calls == [COURSE, COURSE, COURSE, COURSE]


def test_success_after_failures_recovers_and_resets_failure_count(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")

    scraper = FakeScraper({COURSE: FetchError("down")})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))
    assert repository.get_section_state(section).consecutive_failures == 1

    scraper.set_response(COURSE, [snapshot("001", 5, observed_at=dt(1))])
    poller.run(watches, now=dt(1))

    state = repository.get_section_state(section)
    assert state.consecutive_failures == 0
    assert state.seats_available == 5


def test_watched_section_missing_from_response_is_skipped_not_crashed(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "999")]
    scraper = FakeScraper({COURSE: [snapshot("001", 3, observed_at=dt(0))]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert summary.sections_observed == 0
    assert summary.courses_fetched == 1
    missing_section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "999")
    assert repository.get_section_state(missing_section) is None


def test_transition_to_available_notifies_through_full_stack(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))  # first observation, no notify
    assert notifier.events == []

    scraper.set_response(COURSE, [snapshot("001", 4, observed_at=dt(1))])
    poller.run(watches, now=dt(1))

    assert len(notifier.events) == 1


def test_course_wide_watch_processes_every_returned_section(repository, config):
    """section omitted -> all returned sections processed."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number)]
    scraper = FakeScraper(
        {
            COURSE: [
                snapshot("001", 3, observed_at=dt(0)),
                snapshot("002", 0, observed_at=dt(0)),
                snapshot("003", 7, observed_at=dt(0)),
            ]
        }
    )
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert scraper.calls == [COURSE]  # still one fetch per distinct course
    assert summary.courses_fetched == 1
    assert summary.sections_observed == 3
    for section in ("001", "002", "003"):
        key = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, section)
        assert repository.get_section_state(key) is not None
    # first observation for all three -> no notifications yet
    assert notifier.events == []


def test_section_specific_watch_processes_only_that_section(repository, config):
    """section specified -> only that section is processed, even though the course
    fetch returns other sections too."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "002")]
    scraper = FakeScraper(
        {
            COURSE: [
                snapshot("001", 3, observed_at=dt(0)),
                snapshot("002", 0, observed_at=dt(0)),
                snapshot("003", 7, observed_at=dt(0)),
            ]
        }
    )
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert summary.sections_observed == 1
    watched = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "002")
    unwatched_1 = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    unwatched_3 = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "003")
    assert repository.get_section_state(watched) is not None
    assert repository.get_section_state(unwatched_1) is None
    assert repository.get_section_state(unwatched_3) is None


def test_overlapping_course_wide_and_section_specific_watch_is_not_processed_twice(
    repository, config
):
    """A course-wide watch and a section-specific watch on the same course, both
    covering section 001, must still only touch state/observations/notifications once."""
    watches = [
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number),  # course-wide
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001"),  # overlaps
    ]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))
    assert summary.sections_observed == 1
    assert scraper.calls == [COURSE]  # exactly one fetch for the shared course

    # Now drive a real transition and confirm exactly one notification, not two.
    scraper.set_response(COURSE, [snapshot("001", 5, observed_at=dt(1))])
    summary2 = poller.run(watches, now=dt(1))

    assert summary2.sections_observed == 1
    assert len(notifier.events) == 1
    assert len(repository.list_observations(section)) == 2  # one append per poll, not per watch


def test_course_wide_watch_prefers_explicit_watchs_policy_on_overlap(repository, config):
    """The section-specific entry's waitlist_policy wins over the course-wide one when
    both cover the same section."""
    watches = [
        WatchEntry(
            COURSE.term, COURSE.subject, COURSE.catalog_number,
            waitlist_policy=WaitlistPolicy.ALERT,
        ),
        WatchEntry(
            COURSE.term, COURSE.subject, COURSE.catalog_number, "001",
            waitlist_policy=WaitlistPolicy.SILENT,
        ),
    ]
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)
    poller.run(watches, now=dt(0))  # first observation

    scraper.set_response(
        COURSE,
        [
            SeatSnapshot(
                section_key=SectionKey(
                    COURSE.term, COURSE.subject, COURSE.catalog_number, "001"
                ),
                seats_available=1,
                seats_capacity=20,
                waitlist_available=2,
                waitlist_capacity=10,  # 8 people on the waitlist -> policy matters
                status="open",
                observed_at=dt(1),
            )
        ],
    )
    poller.run(watches, now=dt(1))

    # SILENT (from the section-specific entry) wins, so no notification fires despite
    # the course-wide entry saying ALERT.
    assert notifier.events == []


def test_empty_course_response_is_a_valid_empty_result_not_a_failure(repository, config):
    """no returned sections -> valid empty result, not a fetch failure."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number)]
    scraper = FakeScraper({COURSE: []})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert summary.courses_fetched == 1
    assert summary.courses_failed == 0
    assert summary.sections_observed == 0
    assert notifier.events == []


# --- Course-level fetch health for course-wide (no pinned section) watches ---------


def test_course_wide_watch_first_fetch_failure_is_recorded_at_course_level(repository, config):
    """A course-wide watch fails on its very first fetch -> the failure is counted at
    the course level even though no section has ever been observed."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number)]
    scraper = FakeScraper({COURSE: FetchError("simulated outage")})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary = poller.run(watches, now=dt(0))

    assert summary.courses_failed == 1
    health = repository.get_course_fetch_health(COURSE)
    assert health is not None
    assert health.consecutive_failures == 1
    assert health.last_error == "simulated outage"
    # Still no synthetic section row anywhere for this course.
    assert repository.list_section_states_for_course(COURSE) == []


def test_repeated_initial_failures_open_circuit_breaker_for_course_wide_watch(
    repository, config
):
    """Before this fix, a course-wide watch that always failed could never trip the
    breaker (nothing to track failures against). Now it opens just like an explicit
    watch would."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number)]
    scraper = FakeScraper({COURSE: FetchError("down")})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))
    poller.run(watches, now=dt(1))
    summary3 = poller.run(watches, now=dt(2))  # threshold=3 from the `config` fixture

    assert summary3.courses_failed == 1
    health = repository.get_course_fetch_health(COURSE)
    assert health.consecutive_failures == 3
    assert scraper.calls == [COURSE, COURSE, COURSE]

    # Still within the backoff window -> skipped, no new fetch attempt.
    summary4 = poller.run(watches, now=dt(2) + timedelta(seconds=30))
    assert summary4.courses_skipped_circuit_open == 1
    assert scraper.calls == [COURSE, COURSE, COURSE]

    # Backoff elapsed -> attempted again.
    summary5 = poller.run(watches, now=dt(2) + timedelta(seconds=61))
    assert summary5.courses_skipped_circuit_open == 0
    assert summary5.courses_failed == 1
    assert scraper.calls == [COURSE, COURSE, COURSE, COURSE]


def test_successful_fetch_resets_course_level_failures(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number)]
    scraper = FakeScraper({COURSE: FetchError("down")})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))
    assert repository.get_course_fetch_health(COURSE).consecutive_failures == 1

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    poller.run(watches, now=dt(1))

    health = repository.get_course_fetch_health(COURSE)
    assert health.consecutive_failures == 0
    assert health.next_retry_at is None
    assert health.last_error is None


def test_section_state_and_observations_remain_empty_after_repeated_failures(
    repository, config
):
    """A course-wide watch that only ever fails must never produce a section_state row
    or an observation - fetch health is orchestration bookkeeping, not seat data."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number)]
    scraper = FakeScraper({COURSE: FetchError("down")})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    summary1 = poller.run(watches, now=dt(0))
    summary2 = poller.run(watches, now=dt(1))

    assert summary1.sections_observed == 0
    assert summary2.sections_observed == 0
    assert notifier.events == []
    assert repository.list_section_states_for_course(COURSE) == []
    # No section key has ever been created, so any lookup for one comes back empty too.
    guessed_section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    assert repository.get_section_state(guessed_section) is None
    assert repository.list_observations(guessed_section) == []


def test_course_specific_watch_failure_does_not_affect_other_courses_fetch_health(
    repository, config
):
    """Course-level fetch health is isolated per course: one course failing (whether
    watched course-wide or by explicit section) must not touch another course's health
    or section state."""
    watches = [
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001"),
        WatchEntry(OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number),
    ]
    other_snapshot = SeatSnapshot(
        section_key=SectionKey(
            OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number, "001"
        ),
        seats_available=2,
        seats_capacity=30,
        waitlist_available=5,
        waitlist_capacity=5,
        status="open",
        observed_at=dt(0),
    )
    scraper = FakeScraper({COURSE: FetchError("simulated outage"), OTHER_COURSE: [other_snapshot]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))

    failing_health = repository.get_course_fetch_health(COURSE)
    assert failing_health is not None
    assert failing_health.consecutive_failures == 1

    healthy_health = repository.get_course_fetch_health(OTHER_COURSE)
    assert healthy_health is not None
    assert healthy_health.consecutive_failures == 0

    healthy_section = SectionKey(
        OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number, "001"
    )
    assert repository.get_section_state(healthy_section).seats_available == 2


# --- SMTP failure handling and notification retry ------------------------------------


def test_smtp_failure_does_not_update_last_notified_at(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))  # first observation, nothing to notify

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    summary = poller.run(watches, now=dt(1))  # transition -> attempt -> SMTP fails

    assert summary.notification_failures == 1
    assert summary.sections_notified == 0
    state = repository.get_section_state(section)
    assert state is not None
    assert state.last_notified_at is None
    assert state.pending_notification_since == dt(1)


def test_smtp_failure_does_not_abort_other_sections_or_courses(repository, config):
    section_a = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    section_b = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "002")
    other_section = SectionKey(
        OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number, "001"
    )
    watches = [
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001"),
        WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "002"),
        WatchEntry(OTHER_COURSE.term, OTHER_COURSE.subject, OTHER_COURSE.catalog_number, "001"),
    ]

    def other_snapshot(seats, *, observed_at):
        return SeatSnapshot(
            section_key=other_section,
            seats_available=seats,
            seats_capacity=10,
            waitlist_available=5,
            waitlist_capacity=5,
            status="open" if seats else "closed",
            observed_at=observed_at,
        )

    scraper = FakeScraper(
        {
            COURSE: [
                snapshot("001", 0, observed_at=dt(0)),
                snapshot("002", 0, observed_at=dt(0)),
            ],
            OTHER_COURSE: [other_snapshot(0, observed_at=dt(0))],
        }
    )
    # Only the very first notify() call fails - whichever section that turns out to be.
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)
    poller.run(watches, now=dt(0))  # first observation for all three - nothing attempted

    scraper.set_response(
        COURSE,
        [snapshot("001", 3, observed_at=dt(1)), snapshot("002", 4, observed_at=dt(1))],
    )
    scraper.set_response(OTHER_COURSE, [other_snapshot(2, observed_at=dt(1))])
    summary = poller.run(watches, now=dt(1))

    # All three sections, across two courses, were still processed this poll.
    assert summary.sections_observed == 3
    assert summary.notification_failures == 1
    assert summary.sections_notified == 2

    states = [repository.get_section_state(k) for k in (section_a, section_b, other_section)]
    assert all(s is not None for s in states)
    failed = [s for s in states if s.last_notified_at is None]
    succeeded = [s for s in states if s.last_notified_at is not None]
    assert len(failed) == 1
    assert len(succeeded) == 2


def test_failed_notification_retries_on_later_poll_while_still_available(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))  # first observation

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    poller.run(watches, now=dt(1))  # transition -> attempt -> fails
    assert len(notifier.attempts) == 1
    assert notifier.events == []

    # notification_retry_delay is 5 minutes (config fixture) - only 2 minutes have passed.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(3))])
    poller.run(watches, now=dt(3))
    assert len(notifier.attempts) == 1  # not retried yet

    # 6 minutes since the failed attempt - due for retry.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(7))])
    poller.run(watches, now=dt(7))
    assert len(notifier.attempts) == 2  # retried
    assert len(notifier.events) == 1  # and this one succeeds (fail_times exhausted)


def test_successful_retry_updates_last_notified_at_exactly_once(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    poller.run(watches, now=dt(1))  # fails
    assert repository.get_section_state(section).last_notified_at is None

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(7))])
    summary = poller.run(watches, now=dt(7))  # retry due -> succeeds

    assert summary.sections_notified == 1
    state = repository.get_section_state(section)
    assert state.last_notified_at == dt(7)
    assert state.pending_notification_since is None
    # Kept, not cleared: a record of when the successful delivery actually happened.
    assert state.last_notification_attempt_at == dt(7)
    assert len(notifier.events) == 1  # exactly one successful send total

    # Still available on the next poll - must not fire again.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(8))])
    poller.run(watches, now=dt(8))
    assert len(notifier.events) == 1  # unchanged - no duplicate


def test_section_closing_before_retry_clears_pending_notification(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    poller.run(watches, now=dt(1))  # fails, pending set

    state = repository.get_section_state(section)
    assert state.pending_notification_since is not None

    # Section closes before the retry delay elapses.
    scraper.set_response(COURSE, [snapshot("001", 0, observed_at=dt(2))])
    poller.run(watches, now=dt(2))

    state = repository.get_section_state(section)
    assert state.pending_notification_since is None
    assert state.last_notification_attempt_at is None
    assert state.last_notified_at is None

    # Even well past where a retry would have been due, nothing fires while unavailable.
    scraper.set_response(COURSE, [snapshot("001", 0, observed_at=dt(10))])
    poller.run(watches, now=dt(10))
    assert len(notifier.attempts) == 1  # still just the one original attempt

    # A brand-new transition later notifies fresh, not as a leftover retry.
    scraper.set_response(COURSE, [snapshot("001", 5, observed_at=dt(11))])
    poller.run(watches, now=dt(11))
    assert len(notifier.attempts) == 2
    assert len(notifier.events) == 1


def test_observation_and_seat_state_persisted_when_smtp_fails(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    summary = poller.run(watches, now=dt(1))

    assert summary.notification_failures == 1
    state = repository.get_section_state(section)
    assert state is not None
    assert state.seats_available == 3
    assert state.last_seen_at == dt(1)
    history = repository.list_observations(section)
    assert [o.seats_available for o in history] == [0, 3]


def test_notification_failure_is_logged(repository, config, caplog):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1, exception=RuntimeError("smtp connection refused"))
    poller = Poller(scraper, repository, notifier, config)

    poller.run(watches, now=dt(0))

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    with caplog.at_level(logging.ERROR, logger="unc_notifier.poller"):
        poller.run(watches, now=dt(1))

    failure_records = [
        r for r in caplog.records if r.getMessage().startswith("notification_failed")
    ]
    assert len(failure_records) == 1
    record = failure_records[0]
    message = record.getMessage()
    assert "COMP" in message  # course
    assert "550" in message
    assert "001" in message  # section
    assert "RuntimeError" in message  # exception type
    assert record.exc_info is not None  # traceback captured


# --- last_notification_attempt_at reflects the real attempt, not the decision -------


def test_successful_send_records_actual_attempt_time_not_snapshot_time(repository, config):
    """last_notification_attempt_at must come from the poller's own clock (`now`), not
    from snapshot.observed_at - the two are different clocks and can disagree."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)
    poller.run(watches, now=dt(0))

    # The data claims minute 1; the poll (and thus the notify attempt) actually
    # happens at minute 20 - e.g. a delayed run.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    attempt_clock = dt(20)
    summary = poller.run(watches, now=attempt_clock)

    assert summary.sections_notified == 1
    state = repository.get_section_state(section)
    assert state.last_notified_at == attempt_clock
    assert state.last_notification_attempt_at == attempt_clock
    assert state.last_notification_attempt_at != dt(1)  # not snapshot.observed_at


def test_failed_send_records_actual_attempt_time_not_snapshot_time(repository, config):
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)
    poller.run(watches, now=dt(0))

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    attempt_clock = dt(20)
    summary = poller.run(watches, now=attempt_clock)

    assert summary.notification_failures == 1
    state = repository.get_section_state(section)
    assert state.last_notification_attempt_at == attempt_clock
    assert state.last_notification_attempt_at != dt(1)  # not snapshot.observed_at
    assert state.last_notified_at is None
    assert state.pending_notification_since is not None


def test_retry_delay_measured_from_actual_attempt_time(repository, config):
    """Retry gating must use the real attempt clock recorded by the poller, not the
    stale data timestamp from the poll where the attempt failed."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier(fail_times=1)
    poller = Poller(scraper, repository, notifier, config)
    poller.run(watches, now=dt(0))

    # Failed attempt: snapshot says minute 1, but the real attempt clock is minute 20.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    poller.run(watches, now=dt(20))
    assert len(notifier.attempts) == 1
    assert repository.get_section_state(section).last_notification_attempt_at == dt(20)

    # 3 minutes after the real attempt (dt(23)); retry_delay is 5 minutes - not due.
    # Measured from the stale snapshot time (dt(1)) instead, this would look 22+
    # minutes overdue and retry far too early.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(23))])
    poller.run(watches, now=dt(23))
    assert len(notifier.attempts) == 1  # correctly still not due

    # 6 minutes after the real attempt (dt(26)): due.
    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(26))])
    poller.run(watches, now=dt(26))
    assert len(notifier.attempts) == 2  # retried


def test_interrupted_before_notify_call_cannot_persist_attempt_timestamp(
    repository, config, monkeypatch
):
    """If something kills execution after the state machine decides an attempt is due
    but before notifier.notify() is ever reached, nothing about that attempt may reach
    the database - persistence only happens after _attempt_notify resolves, and
    _attempt_notify never resolves without at least trying notify()."""
    watches = [WatchEntry(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")]
    section = SectionKey(COURSE.term, COURSE.subject, COURSE.catalog_number, "001")
    scraper = FakeScraper({COURSE: [snapshot("001", 0, observed_at=dt(0))]})
    notifier = FakeNotifier()
    poller = Poller(scraper, repository, notifier, config)
    poller.run(watches, now=dt(0))  # first observation, nothing pending

    # Simulate the process dying between the state-machine decision and the actual
    # notifier.notify() call, by making the outgoing event blow up before notify() can
    # ever be reached.
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated interruption before notify()")

    monkeypatch.setattr("unc_notifier.poller.NotificationEvent", _boom)

    scraper.set_response(COURSE, [snapshot("001", 3, observed_at=dt(1))])
    with pytest.raises(RuntimeError, match="simulated interruption"):
        poller.run(watches, now=dt(1))

    assert notifier.attempts == []  # notify() was truly never reached

    # Nothing about this interrupted poll was persisted - state is exactly what the
    # first observation left behind.
    state = repository.get_section_state(section)
    assert state.last_notification_attempt_at is None
    assert state.pending_notification_since is None
    assert state.last_notified_at is None
    assert [o.seats_available for o in repository.list_observations(section)] == [0]
