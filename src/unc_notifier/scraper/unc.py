"""Real scraper for UNC's public class search, using Tiled Search as the data source
(https://reports.unc.edu/class-search/tiled/) - see SPEC.md for the captured request
flow and tests/fixtures/unc_class_search_tiled_*.html for real, sanitized responses.

Tiled Search was chosen over Standard/Advanced Search because it's the only public
view that exposes seat *capacity* (``N/M available seats``), not just seats available.
Waitlist data is confirmed absent from all three public views - see
``unc_notifier.models.SeatSnapshot.waitlist_status_unknown``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from unc_notifier.models import CourseKey, SeatSnapshot
from unc_notifier.scraper.base import FetchError, Scraper
from unc_notifier.scraper.tiled_parser import extract_csrf_token, parse_tiled_search

_BASE_URL = "https://reports.unc.edu"
_TILED_PATH = "/class-search/tiled/"
_TILED_URL = _BASE_URL + _TILED_PATH
_REQUEST_TIMEOUT_SECONDS = 30


class UncClassSearchScraper(Scraper):
    """Owns one requests.Session for its whole lifetime. Django's CSRF token is tied
    to the session (cookie), not single-use per request, so a single GET's token is
    reused for every subsequent POST - the session is primed at most once regardless
    of how many distinct courses are fetched through it. Since the CLI is a
    single-shot cron script (one process = one poll run = one scraper instance),
    "prime once per run" falls out for free from "prime once per process."
    """

    def __init__(self, *, contact_email: str, session: requests.Session | None = None) -> None:
        self._contact_email = contact_email
        self._session = session if session is not None else requests.Session()
        self._csrf_token: str | None = None

    def fetch_course(self, course_key: CourseKey) -> list[SeatSnapshot]:
        self._ensure_primed()
        response = self._post(
            _TILED_URL,
            data={
                "csrfmiddlewaretoken": self._csrf_token,
                "term": course_key.term,
                "subject": course_key.subject,
                "catalog_number": course_key.catalog_number,
            },
        )
        return parse_tiled_search(response.text, observed_at=datetime.now(UTC))

    def _ensure_primed(self) -> None:
        if self._csrf_token is not None:
            return
        response = self._get(_TILED_URL)
        self._csrf_token = extract_csrf_token(response.text)

    def _get(self, url: str):
        try:
            response = self._session.get(
                url, headers=self._headers(), timeout=_REQUEST_TIMEOUT_SECONDS
            )
        except requests.exceptions.RequestException as exc:
            raise FetchError(f"GET {url} failed: {exc}") from exc
        return self._checked(response, url, method="GET")

    def _post(self, url: str, *, data: dict[str, str]):
        try:
            response = self._session.post(
                url,
                data=data,
                headers=self._headers(referer=url),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            raise FetchError(f"POST {url} failed: {exc}") from exc
        return self._checked(response, url, method="POST")

    @staticmethod
    def _checked(response, url: str, *, method: str):
        if response.status_code != 200:
            raise FetchError(f"{method} {url} returned HTTP {response.status_code}")
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            raise FetchError(
                f"{method} {url} returned unexpected Content-Type {content_type!r}"
            )
        return response

    def _headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = {"User-Agent": f"unc-seat-notifier/0.1 (contact: {self._contact_email})"}
        if referer is not None:
            headers["Referer"] = referer
        return headers
