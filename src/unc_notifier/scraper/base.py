"""Abstract scraper interface.

The rest of the system depends only on this interface, never on a concrete
implementation. Until UNC's real endpoint (URL, params, response schema) has been
captured from a browser and handed over, the only implementation that exists is the
fixture-backed :class:`~unc_notifier.scraper.fake.FakeScraper`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from unc_notifier.models import CourseKey, SeatSnapshot


class FetchError(Exception):
    """Raised for any failed or malformed fetch: network error, non-2xx, bad JSON,
    unexpected schema. The poller must treat this uniformly as "no new information" —
    never as zero seats and never as a state change."""


class Scraper(ABC):
    """Fetches current seat data for one course, keyed at (term, subject, catalog_number).

    One call returns every watched section under that course, so the poller can dedupe
    by course rather than by individual section.
    """

    @abstractmethod
    def fetch_course(self, course_key: CourseKey) -> list[SeatSnapshot]:
        """Return the current snapshot for every section of ``course_key``.

        Raises FetchError on any failure. Never returns a partial/best-effort result for
        a course it couldn't fully fetch.
        """
        raise NotImplementedError
