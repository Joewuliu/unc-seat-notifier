"""SQLite persistence for section state and the append-only observation history.

Plain stdlib ``sqlite3`` behind a small repository class, not an ORM: the schema is three
tables and single-user, so SQLAlchemy would add an abstraction layer with no real payoff
here. Domain code never imports ``sqlite3`` directly — it only sees this module's
dataclass-in, dataclass-out API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from unc_notifier.models import (
    CourseFetchHealth,
    CourseKey,
    SeatSnapshot,
    SectionKey,
    SectionState,
)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _row_to_state(row: sqlite3.Row) -> SectionState:
    return SectionState(
        section_key=SectionKey(
            term=row["term"],
            subject=row["subject"],
            catalog_number=row["catalog_number"],
            section=row["section"],
        ),
        seats_available=row["seats_available"],
        seats_capacity=row["seats_capacity"],
        waitlist_available=row["waitlist_available"],
        waitlist_capacity=row["waitlist_capacity"],
        status=row["status"],
        last_seen_at=_from_iso(row["last_seen_at"]),
        last_notified_at=_from_iso(row["last_notified_at"]),
        consecutive_failures=row["consecutive_failures"],
        last_attempt_at=_from_iso(row["last_attempt_at"]),
        pending_notification_since=_from_iso(row["pending_notification_since"]),
        last_notification_attempt_at=_from_iso(row["last_notification_attempt_at"]),
    )


def _row_to_course_health(row: sqlite3.Row) -> CourseFetchHealth:
    return CourseFetchHealth(
        course_key=CourseKey(
            term=row["term"], subject=row["subject"], catalog_number=row["catalog_number"]
        ),
        consecutive_failures=row["consecutive_failures"],
        last_attempt_at=_from_iso(row["last_attempt_at"]),
        next_retry_at=_from_iso(row["next_retry_at"]),
        last_error=row["last_error"],
    )


class Repository:
    """One method per operation the poller needs. All datetimes in and out are
    timezone-aware; storage format is ISO 8601 text."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def ensure_schema(self) -> None:
        self._conn.executescript(_SCHEMA_PATH.read_text())
        self._conn.commit()

    def get_section_state(self, section_key: SectionKey) -> SectionState | None:
        row = self._conn.execute(
            "SELECT * FROM section_state WHERE term = ? AND subject = ? "
            "AND catalog_number = ? AND section = ?",
            (section_key.term, section_key.subject, section_key.catalog_number,
             section_key.section),
        ).fetchone()
        return _row_to_state(row) if row is not None else None

    def list_section_states_for_course(self, course_key: CourseKey) -> list[SectionState]:
        """Every section row we already know about for this (term, subject,
        catalog_number) - used to resolve which real sections a course-wide watch
        covers, without ever inventing a synthetic "whole course" row."""
        rows = self._conn.execute(
            "SELECT * FROM section_state WHERE term = ? AND subject = ? AND catalog_number = ?",
            (course_key.term, course_key.subject, course_key.catalog_number),
        ).fetchall()
        return [_row_to_state(row) for row in rows]

    def get_observed_section_state(self, section_key: SectionKey) -> SectionState | None:
        """Like get_section_state, but returns None if the row only holds failure
        bookkeeping and has never had a successful observation — i.e. what the state
        machine should treat as "no prior state"."""
        state = self.get_section_state(section_key)
        if state is None or state.last_seen_at is None:
            return None
        return state

    def save_state(self, state: SectionState) -> None:
        """Upsert the full row after a successful fetch + diff.evaluate() call."""
        key = state.section_key
        self._conn.execute(
            """
            INSERT INTO section_state (
                term, subject, catalog_number, section,
                seats_available, seats_capacity, waitlist_available, waitlist_capacity,
                status, last_seen_at, last_notified_at, consecutive_failures, last_attempt_at,
                pending_notification_since, last_notification_attempt_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (term, subject, catalog_number, section) DO UPDATE SET
                seats_available = excluded.seats_available,
                seats_capacity = excluded.seats_capacity,
                waitlist_available = excluded.waitlist_available,
                waitlist_capacity = excluded.waitlist_capacity,
                status = excluded.status,
                last_seen_at = excluded.last_seen_at,
                last_notified_at = excluded.last_notified_at,
                consecutive_failures = excluded.consecutive_failures,
                last_attempt_at = excluded.last_attempt_at,
                pending_notification_since = excluded.pending_notification_since,
                last_notification_attempt_at = excluded.last_notification_attempt_at
            """,
            (
                key.term, key.subject, key.catalog_number, key.section,
                state.seats_available, state.seats_capacity,
                state.waitlist_available, state.waitlist_capacity,
                state.status, _to_iso(state.last_seen_at), _to_iso(state.last_notified_at),
                state.consecutive_failures, _to_iso(state.last_attempt_at),
                _to_iso(state.pending_notification_since),
                _to_iso(state.last_notification_attempt_at),
            ),
        )
        self._conn.commit()

    def record_failure(self, section_key: SectionKey, *, attempt_at: datetime) -> SectionState:
        """Bump consecutive_failures and last_attempt_at only. Every other field —
        including a never-set last_seen_at — is left exactly as it was, so a failed
        fetch can never look like a state change."""
        key = section_key
        self._conn.execute(
            """
            INSERT INTO section_state (
                term, subject, catalog_number, section, consecutive_failures, last_attempt_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT (term, subject, catalog_number, section) DO UPDATE SET
                consecutive_failures = consecutive_failures + 1,
                last_attempt_at = excluded.last_attempt_at
            """,
            (key.term, key.subject, key.catalog_number, key.section, _to_iso(attempt_at)),
        )
        self._conn.commit()
        state = self.get_section_state(section_key)
        assert state is not None
        return state

    def get_course_fetch_health(self, course_key: CourseKey) -> CourseFetchHealth | None:
        row = self._conn.execute(
            "SELECT * FROM course_fetch_health WHERE term = ? AND subject = ? "
            "AND catalog_number = ?",
            (course_key.term, course_key.subject, course_key.catalog_number),
        ).fetchone()
        return _row_to_course_health(row) if row is not None else None

    def record_course_fetch_failure(
        self,
        course_key: CourseKey,
        *,
        attempt_at: datetime,
        next_retry_at: datetime | None,
        error: str,
    ) -> CourseFetchHealth:
        """Bump the course-level failure counter. Never touches any section_state row -
        this is fetch-orchestration bookkeeping only, keyed at the course level so a
        course-wide watch's failures are tracked even before any real section under it
        has ever been observed."""
        key = course_key
        self._conn.execute(
            """
            INSERT INTO course_fetch_health (
                term, subject, catalog_number, consecutive_failures,
                last_attempt_at, next_retry_at, last_error
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT (term, subject, catalog_number) DO UPDATE SET
                consecutive_failures = consecutive_failures + 1,
                last_attempt_at = excluded.last_attempt_at,
                next_retry_at = excluded.next_retry_at,
                last_error = excluded.last_error
            """,
            (key.term, key.subject, key.catalog_number, _to_iso(attempt_at),
             _to_iso(next_retry_at), error),
        )
        self._conn.commit()
        health = self.get_course_fetch_health(course_key)
        assert health is not None
        return health

    def record_course_fetch_success(self, course_key: CourseKey, *, attempt_at: datetime) -> None:
        """Reset the course-level failure counter after a successful fetch."""
        key = course_key
        self._conn.execute(
            """
            INSERT INTO course_fetch_health (
                term, subject, catalog_number, consecutive_failures,
                last_attempt_at, next_retry_at, last_error
            ) VALUES (?, ?, ?, 0, ?, NULL, NULL)
            ON CONFLICT (term, subject, catalog_number) DO UPDATE SET
                consecutive_failures = 0,
                last_attempt_at = excluded.last_attempt_at,
                next_retry_at = NULL,
                last_error = NULL
            """,
            (key.term, key.subject, key.catalog_number, _to_iso(attempt_at)),
        )
        self._conn.commit()

    def record_observation(self, snapshot: SeatSnapshot) -> None:
        """Append-only insert. Only ever called for a successful, validated fetch."""
        key = snapshot.section_key
        self._conn.execute(
            """
            INSERT INTO observations (
                term, subject, catalog_number, section,
                seats_available, seats_capacity, waitlist_available, waitlist_capacity,
                status, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key.term, key.subject, key.catalog_number, key.section,
                snapshot.seats_available, snapshot.seats_capacity,
                snapshot.waitlist_available, snapshot.waitlist_capacity,
                snapshot.status, _to_iso(snapshot.observed_at),
            ),
        )
        self._conn.commit()

    def list_observations(self, section_key: SectionKey) -> list[SeatSnapshot]:
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE term = ? AND subject = ? "
            "AND catalog_number = ? AND section = ? ORDER BY observed_at",
            (section_key.term, section_key.subject, section_key.catalog_number,
             section_key.section),
        ).fetchall()
        return [
            SeatSnapshot(
                section_key=section_key,
                seats_available=row["seats_available"],
                seats_capacity=row["seats_capacity"],
                waitlist_available=row["waitlist_available"],
                waitlist_capacity=row["waitlist_capacity"],
                status=row["status"],
                observed_at=_from_iso(row["observed_at"]),
            )
            for row in rows
        ]
