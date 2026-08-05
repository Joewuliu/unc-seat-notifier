"""Placeholder for the real UNC class-search scraper.

Not implemented. Per SPEC.md, the real request URL, query parameters, and JSON response
schema for https://reports.unc.edu/class-search/ must be captured from a browser's
DevTools Network tab and handed over before this can be written — guessing the schema is
explicitly disallowed. Wired into the CLI now so swapping it in later is a one-line change,
but calling it will always fail loudly until that happens.
"""

from __future__ import annotations

from unc_notifier.models import CourseKey, SeatSnapshot
from unc_notifier.scraper.base import Scraper


class UncClassSearchScraper(Scraper):
    def __init__(self, *, contact_email: str) -> None:
        self._contact_email = contact_email

    def fetch_course(self, course_key: CourseKey) -> list[SeatSnapshot]:
        raise NotImplementedError(
            "The real UNC class-search scraper has not been implemented yet. "
            "It requires a real captured request + JSON response from "
            "https://reports.unc.edu/class-search/ (see SPEC.md). "
            "Use unc_notifier.scraper.fake.FakeScraper until then."
        )
