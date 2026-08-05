"""Orchestration layer: dedup watched courses, fetch, diff, persist, notify.

This is where network failures and malformed responses are actually handled — the pure
``diff`` module is never told about them; it only ever sees successful, validated
snapshots. A course whose fetch fails never reaches ``diff.evaluate`` at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from unc_notifier import diff
from unc_notifier.models import (
    CourseFetchHealth,
    CourseKey,
    Decision,
    NotificationEvent,
    SeatSnapshot,
    SectionKey,
    SectionState,
    WaitlistPolicy,
    WatchEntry,
)
from unc_notifier.notifier.base import Notifier
from unc_notifier.scraper.base import Scraper
from unc_notifier.state.repository import Repository

logger = logging.getLogger("unc_notifier.poller")


@dataclass(frozen=True, slots=True)
class PollerConfig:
    cooldown: timedelta
    default_waitlist_policy: WaitlistPolicy
    circuit_breaker_threshold: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    notification_retry_delay: timedelta


@dataclass
class PollSummary:
    courses_fetched: int = 0
    courses_skipped_circuit_open: int = 0
    courses_failed: int = 0
    sections_observed: int = 0
    sections_notified: int = 0
    notification_failures: int = 0
    errors: list[str] = field(default_factory=list)


class Poller:
    def __init__(
        self,
        scraper: Scraper,
        repository: Repository,
        notifier: Notifier,
        config: PollerConfig,
    ) -> None:
        self._scraper = scraper
        self._repository = repository
        self._notifier = notifier
        self._config = config

    def run(self, watches: list[WatchEntry], *, now: datetime) -> PollSummary:
        summary = PollSummary()
        for course_key, course_watch in _group_by_course(watches).items():
            self._poll_course(course_key, course_watch, now=now, summary=summary)

        return summary

    def _poll_course(
        self,
        course_key: CourseKey,
        course_watch: _CourseWatch,
        *,
        now: datetime,
        summary: PollSummary,
    ) -> None:
        health = self._repository.get_course_fetch_health(course_key)
        if _course_health_circuit_open(health, now, self._config):
            summary.courses_skipped_circuit_open += 1
            logger.warning(
                "circuit_open course=%s failures=%d",
                course_key,
                health.consecutive_failures if health else 0,
            )
            return

        # Known state for every section we might need to touch: explicit watches always
        # (even if never yet observed - the row may just hold failure bookkeeping), plus,
        # for a course-wide watch, whatever real sections we've already discovered for
        # this course. We never invent a state row for "the course as a whole" - that's
        # what course_fetch_health (queried above) is for.
        known_states = {
            key: self._repository.get_section_state(key) for key in course_watch.explicit
        }
        if course_watch.wildcard is not None:
            for state in self._repository.list_section_states_for_course(course_key):
                known_states.setdefault(state.section_key, state)

        try:
            snapshots = self._scraper.fetch_course(course_key)
            _validate_snapshots(snapshots, course_key)
        except Exception as exc:  # noqa: BLE001 - any failure here means "no new data"
            summary.courses_failed += 1
            summary.errors.append(str(exc))
            logger.error("fetch_failed course=%s error=%s", course_key, exc)

            failures_so_far = (health.consecutive_failures if health else 0) + 1
            next_retry_at = now + timedelta(
                seconds=_compute_backoff_seconds(failures_so_far, self._config)
            )
            course_health = self._repository.record_course_fetch_failure(
                course_key, attempt_at=now, next_retry_at=next_retry_at, error=str(exc)
            )
            if course_health.consecutive_failures == self._config.circuit_breaker_threshold:
                logger.error(
                    "circuit_breaker_tripped course=%s failures=%d",
                    course_key,
                    course_health.consecutive_failures,
                )

            # Per-section failure counts (SPEC.md's data model) for sections we already
            # have a real handle on - never create a row just because a course-wide
            # watch's fetch failed and we don't yet know its sections.
            for section_key in known_states:
                self._repository.record_failure(section_key, attempt_at=now)
            return

        self._repository.record_course_fetch_success(course_key, attempt_at=now)
        summary.courses_fetched += 1
        snapshots_by_section = {snap.section_key: snap for snap in snapshots}
        returned_keys = set(snapshots_by_section)

        # A dict, so a section covered by both a course-wide and a section-specific
        # watch is keyed once - it's processed and notified exactly once. The
        # section-specific entry (if any) wins for policy resolution, since it's more
        # specific than the course-wide default.
        target_entries: dict[SectionKey, WatchEntry] = {}
        if course_watch.wildcard is not None:
            for section_key in returned_keys:
                target_entries[section_key] = course_watch.wildcard
        for section_key, watch_entry in course_watch.explicit.items():
            if section_key in returned_keys:
                target_entries[section_key] = watch_entry
            else:
                logger.warning(
                    "watched_section_missing_from_response section=%s", section_key
                )

        for section_key, watch_entry in target_entries.items():
            snapshot = snapshots_by_section[section_key]
            self._apply_snapshot(
                snapshot, watch_entry, known_states.get(section_key), summary, now=now
            )

    def _apply_snapshot(
        self,
        snapshot: SeatSnapshot,
        watch_entry: WatchEntry,
        prior_state: SectionState | None,
        summary: PollSummary,
        *,
        now: datetime,
    ) -> None:
        policy = watch_entry.waitlist_policy or self._config.default_waitlist_policy
        prev = prior_state if (prior_state is not None and prior_state.last_seen_at) else None

        decision = diff.evaluate(
            prev,
            snapshot,
            waitlist_policy=policy,
            cooldown=self._config.cooldown,
            retry_delay=self._config.notification_retry_delay,
        )

        # Persisted regardless of what happens below: seat data and observation history
        # must never depend on whether a notification attempt is made or succeeds.
        final_state = decision.new_state
        if decision.should_attempt_notify:
            final_state = self._attempt_notify(snapshot, decision, summary, now=now)

        self._repository.save_state(final_state)
        self._repository.record_observation(snapshot)
        summary.sections_observed += 1

        logger.info(
            "observed section=%s seats=%d/%d reason=%s attempt_notify=%s",
            snapshot.section_key,
            snapshot.seats_available,
            snapshot.seats_capacity,
            decision.reason,
            decision.should_attempt_notify,
        )

    def _attempt_notify(
        self, snapshot: SeatSnapshot, decision: Decision, summary: PollSummary, *, now: datetime
    ) -> SectionState:
        """Try to deliver the notification for a should_attempt_notify decision.

        ``now`` - the poller's own clock, passed in by the caller so tests stay
        deterministic - is stamped onto ``last_notification_attempt_at`` right here,
        immediately before ``notifier.notify()`` is called. This is deliberately never
        ``snapshot.observed_at``: that's when the seat data was fetched, not when this
        notification attempt actually happens. diff.evaluate() never sets this field for
        exactly that reason - only an actual attempt, made here, may.

        Returns the state to persist: on success, last_notified_at advances to ``now``
        and pending_notification_since clears, but last_notification_attempt_at is kept
        (not cleared) as a record of the real attempt time. On failure,
        decision.new_state is returned with only last_notification_attempt_at
        overwritten to ``now`` - everything else it already carries (in particular
        pending_notification_since and the unchanged last_notified_at) is correct for a
        later retry.
        """
        event = NotificationEvent(
            section_key=snapshot.section_key,
            seats_available=snapshot.seats_available,
            seats_capacity=snapshot.seats_capacity,
            waitlist_available=snapshot.waitlist_available,
            waitlist_capacity=snapshot.waitlist_capacity,
            status=snapshot.status,
            reason=decision.reason,
            level=decision.notify_level or diff.LEVEL_INFO,
            occurred_at=snapshot.observed_at,
        )
        attempted_state = replace(decision.new_state, last_notification_attempt_at=now)
        try:
            self._notifier.notify(event)
        except Exception as exc:  # noqa: BLE001 - one section's SMTP failure must not
            # abort any other section or course in this poll run.
            summary.notification_failures += 1
            logger.error(
                "notification_failed course=%s section=%s exception_type=%s error=%s",
                snapshot.section_key.course_key,
                snapshot.section_key,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return attempted_state

        summary.sections_notified += 1
        logger.info("notified section=%s reason=%s", snapshot.section_key, decision.reason)
        return replace(attempted_state, last_notified_at=now, pending_notification_since=None)


@dataclass
class _CourseWatch:
    """Everything watched under one (term, subject, catalog_number)."""

    explicit: dict[SectionKey, WatchEntry] = field(default_factory=dict)
    wildcard: WatchEntry | None = None


def _group_by_course(watches: list[WatchEntry]) -> dict[CourseKey, _CourseWatch]:
    grouped: dict[CourseKey, _CourseWatch] = {}
    for entry in watches:
        course_watch = grouped.setdefault(entry.course_key, _CourseWatch())
        if entry.is_course_wide:
            course_watch.wildcard = entry
        else:
            course_watch.explicit[entry.section_key] = entry
    return grouped


def _validate_snapshots(snapshots: list[SeatSnapshot], course_key: CourseKey) -> None:
    for snapshot in snapshots:
        if snapshot.section_key.course_key != course_key:
            raise ValueError(
                f"scraper returned section {snapshot.section_key!r} that does not "
                f"belong to requested course {course_key!r}"
            )
        if snapshot.seats_available < 0 or snapshot.seats_capacity < 0:
            raise ValueError(f"negative seat counts in snapshot for {snapshot.section_key!r}")


def _course_health_circuit_open(
    health: CourseFetchHealth | None, now: datetime, config: PollerConfig
) -> bool:
    """The course-level circuit only opens once consecutive_failures reaches the
    configured threshold, and only stays open until next_retry_at."""
    if health is None:
        return False
    if health.consecutive_failures < config.circuit_breaker_threshold:
        return False
    if health.next_retry_at is None:
        return False
    return now < health.next_retry_at


def _compute_backoff_seconds(consecutive_failures: int, config: PollerConfig) -> float:
    exponent = max(consecutive_failures - config.circuit_breaker_threshold, 0)
    return min(config.backoff_base_seconds * (2**exponent), config.backoff_max_seconds)
