"""Fixture-backed fake scraper. The only Scraper implementation that exists in Phase 1.

Feed it canned responses (or exceptions) per course, keyed by CourseKey. Every real
component downstream (poller, diff, repository, notifier) is exercised end-to-end through
this fake — no test ever needs a live network call or a guess at UNC's real schema.
"""

from __future__ import annotations

from collections.abc import Callable

from unc_notifier.models import CourseKey, SeatSnapshot
from unc_notifier.scraper.base import FetchError, Scraper

Response = list[SeatSnapshot] | Exception | Callable[[], list[SeatSnapshot]]


class FakeScraper(Scraper):
    def __init__(self, responses: dict[CourseKey, Response] | None = None) -> None:
        self._responses: dict[CourseKey, Response] = dict(responses or {})
        self.calls: list[CourseKey] = []

    def set_response(self, course_key: CourseKey, response: Response) -> None:
        self._responses[course_key] = response

    def fetch_course(self, course_key: CourseKey) -> list[SeatSnapshot]:
        self.calls.append(course_key)
        if course_key not in self._responses:
            raise FetchError(f"FakeScraper has no fixture configured for {course_key!r}")

        response = self._responses[course_key]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response()
        return response
