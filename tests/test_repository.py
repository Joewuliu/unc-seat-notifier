from __future__ import annotations

import sqlite3

import pytest

from unc_notifier.models import CourseKey
from unc_notifier.state.repository import Repository

from .helpers import SECTION, dt, make_snapshot, make_state

COURSE = SECTION.course_key


@pytest.fixture
def repo() -> Repository:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    repository = Repository(conn)
    repository.ensure_schema()
    return repository


def test_get_section_state_returns_none_when_absent(repo: Repository):
    assert repo.get_section_state(SECTION) is None


def test_save_state_round_trips_all_fields(repo: Repository):
    state = make_state(
        seats_available=3,
        seats_capacity=20,
        waitlist_available=7,
        waitlist_capacity=10,
        status="open",
        last_seen_at=dt(0),
        last_notified_at=dt(1),
        consecutive_failures=2,
        last_attempt_at=dt(0),
    )

    repo.save_state(state)
    fetched = repo.get_section_state(SECTION)

    assert fetched == state


def test_save_state_upserts_on_conflict(repo: Repository):
    repo.save_state(make_state(seats_available=0))
    repo.save_state(make_state(seats_available=5, last_seen_at=dt(1)))

    fetched = repo.get_section_state(SECTION)

    assert fetched is not None
    assert fetched.seats_available == 5
    assert fetched.last_seen_at == dt(1)


def test_record_observation_appends_without_touching_prior_rows(repo: Repository):
    repo.record_observation(make_snapshot(seats_available=0, observed_at=dt(0)))
    repo.record_observation(make_snapshot(seats_available=3, observed_at=dt(1)))

    history = repo.list_observations(SECTION)

    assert [obs.seats_available for obs in history] == [0, 3]
    assert [obs.observed_at for obs in history] == [dt(0), dt(1)]


def test_record_failure_creates_row_with_no_seat_state_yet(repo: Repository):
    state = repo.record_failure(SECTION, attempt_at=dt(0))

    assert state.consecutive_failures == 1
    assert state.last_attempt_at == dt(0)
    assert state.last_seen_at is None
    # Never-observed state is invisible to the diff state machine.
    assert repo.get_observed_section_state(SECTION) is None


def test_record_failure_increments_without_touching_existing_seat_state(repo: Repository):
    repo.save_state(
        make_state(seats_available=3, last_seen_at=dt(0), consecutive_failures=0)
    )

    state = repo.record_failure(SECTION, attempt_at=dt(1))

    assert state.consecutive_failures == 1
    assert state.seats_available == 3  # untouched
    assert state.last_seen_at == dt(0)  # untouched
    assert state.last_attempt_at == dt(1)


def test_record_failure_accumulates_across_multiple_calls(repo: Repository):
    repo.record_failure(SECTION, attempt_at=dt(0))
    repo.record_failure(SECTION, attempt_at=dt(1))
    state = repo.record_failure(SECTION, attempt_at=dt(2))

    assert state.consecutive_failures == 3


def test_save_state_after_failures_resets_failure_count(repo: Repository):
    repo.record_failure(SECTION, attempt_at=dt(0))
    repo.record_failure(SECTION, attempt_at=dt(1))

    repo.save_state(make_state(seats_available=1, consecutive_failures=0, last_seen_at=dt(2)))

    state = repo.get_section_state(SECTION)
    assert state is not None
    assert state.consecutive_failures == 0


def test_get_course_fetch_health_returns_none_when_absent(repo: Repository):
    assert repo.get_course_fetch_health(COURSE) is None


def test_record_course_fetch_failure_creates_row_without_any_section_row(repo: Repository):
    health = repo.record_course_fetch_failure(
        COURSE, attempt_at=dt(0), next_retry_at=dt(1), error="boom"
    )

    assert health.consecutive_failures == 1
    assert health.last_attempt_at == dt(0)
    assert health.next_retry_at == dt(1)
    assert health.last_error == "boom"
    # No synthetic section row was created for this course.
    assert repo.list_section_states_for_course(COURSE) == []


def test_record_course_fetch_failure_accumulates_across_calls(repo: Repository):
    repo.record_course_fetch_failure(COURSE, attempt_at=dt(0), next_retry_at=dt(1), error="a")
    repo.record_course_fetch_failure(COURSE, attempt_at=dt(1), next_retry_at=dt(2), error="b")
    health = repo.record_course_fetch_failure(
        COURSE, attempt_at=dt(2), next_retry_at=dt(3), error="c"
    )

    assert health.consecutive_failures == 3
    assert health.last_error == "c"


def test_record_course_fetch_success_resets_failure_count(repo: Repository):
    repo.record_course_fetch_failure(COURSE, attempt_at=dt(0), next_retry_at=dt(1), error="boom")
    repo.record_course_fetch_failure(COURSE, attempt_at=dt(1), next_retry_at=dt(2), error="boom")

    repo.record_course_fetch_success(COURSE, attempt_at=dt(2))

    health = repo.get_course_fetch_health(COURSE)
    assert health is not None
    assert health.consecutive_failures == 0
    assert health.next_retry_at is None
    assert health.last_error is None
    assert health.last_attempt_at == dt(2)


def test_course_fetch_health_is_isolated_per_course(repo: Repository):
    other_course = CourseKey(term=COURSE.term, subject="MATH", catalog_number="231")

    repo.record_course_fetch_failure(COURSE, attempt_at=dt(0), next_retry_at=dt(1), error="boom")

    assert repo.get_course_fetch_health(other_course) is None
