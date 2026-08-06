"""Request-flow tests for UncClassSearchScraper: session priming, CSRF token reuse,
and error handling. Uses a hand-rolled fake requests.Session (consistent with this
project's FakeScraper/FakeNotifier style) - no real network calls, no mocking library.
"""

from __future__ import annotations

import pytest
import requests

from unc_notifier.models import CourseKey
from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.unc import _TILED_URL, UncClassSearchScraper

from .helpers import FIXTURES_DIR

COURSE = CourseKey(term="2026 Fall", subject="COMP", catalog_number="210")

TOKEN_PAGE_HTML = (
    "<html><body><form>"
    '<input type="hidden" name="csrfmiddlewaretoken" value="test-token-123">'
    "</form></body></html>"
)

COMP210_RESULTS_HTML = (FIXTURES_DIR / "unc_class_search_tiled_comp210.html").read_text(
    encoding="utf-8"
)


class FakeResponse:
    def __init__(self, *, status_code=200, text="", content_type="text/html"):
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self._get_queue: list[FakeResponse] = []
        self._post_queue: list[FakeResponse] = []

    def queue_get(self, response: FakeResponse) -> None:
        self._get_queue.append(response)

    def queue_post(self, response: FakeResponse) -> None:
        self._post_queue.append(response)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._get_queue.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._post_queue.pop(0)


def make_scraper(session) -> UncClassSearchScraper:
    return UncClassSearchScraper(contact_email="me@example.com", session=session)


def test_fetch_course_primes_session_then_posts_with_extracted_token():
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(FakeResponse(text=COMP210_RESULTS_HTML))
    scraper = make_scraper(session)

    snapshots = scraper.fetch_course(COURSE)

    assert len(snapshots) == 2
    assert [method for method, _, _ in session.calls] == ["GET", "POST"]
    assert session.calls[0][1] == _TILED_URL  # GET url

    post_url, post_kwargs = session.calls[1][1], session.calls[1][2]
    assert post_url == _TILED_URL
    assert post_kwargs["data"] == {
        "csrfmiddlewaretoken": "test-token-123",
        "term": "2026 Fall",
        "subject": "COMP",
        "catalog_number": "210",
    }


def test_session_and_token_reused_across_multiple_fetch_course_calls():
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(FakeResponse(text=COMP210_RESULTS_HTML))
    session.queue_post(FakeResponse(text=COMP210_RESULTS_HTML))
    scraper = make_scraper(session)

    scraper.fetch_course(COURSE)
    scraper.fetch_course(CourseKey(term="2026 Fall", subject="COMP", catalog_number="211"))

    methods = [method for method, _, _ in session.calls]
    assert methods == ["GET", "POST", "POST"]  # only one GET/priming across both fetches

    # Both POSTs reuse the same token extracted from the single GET.
    assert session.calls[1][2]["data"]["csrfmiddlewaretoken"] == "test-token-123"
    assert session.calls[2][2]["data"]["csrfmiddlewaretoken"] == "test-token-123"


def test_required_user_agent_present_on_get_and_post():
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(FakeResponse(text=COMP210_RESULTS_HTML))
    scraper = make_scraper(session)

    scraper.fetch_course(COURSE)

    for _, _, kwargs in session.calls:
        user_agent = kwargs["headers"]["User-Agent"]
        assert "unc-seat-notifier" in user_agent
        assert "me@example.com" in user_agent


def test_referer_header_set_on_post():
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(FakeResponse(text=COMP210_RESULTS_HTML))
    scraper = make_scraper(session)

    scraper.fetch_course(COURSE)

    assert session.calls[1][2]["headers"]["Referer"] == _TILED_URL


def test_get_non_200_raises_fetch_error_without_posting():
    session = FakeSession()
    session.queue_get(FakeResponse(status_code=403, text="Forbidden"))
    scraper = make_scraper(session)

    with pytest.raises(FetchError, match="GET .* 403"):
        scraper.fetch_course(COURSE)

    assert len(session.calls) == 1  # never attempted the POST


def test_post_non_200_raises_fetch_error():
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(FakeResponse(status_code=403, text="CSRF verification failed"))
    scraper = make_scraper(session)

    with pytest.raises(FetchError, match="POST .* 403"):
        scraper.fetch_course(COURSE)


def test_unexpected_content_type_raises_fetch_error():
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(
        FakeResponse(text='{"error": "not html"}', content_type="application/json")
    )
    scraper = make_scraper(session)

    with pytest.raises(FetchError, match="Content-Type"):
        scraper.fetch_course(COURSE)


def test_network_exception_raises_fetch_error():
    class RaisingSession:
        def get(self, *args, **kwargs):
            raise requests.exceptions.ConnectionError("boom")

    scraper = make_scraper(RaisingSession())

    with pytest.raises(FetchError, match="GET .* failed"):
        scraper.fetch_course(COURSE)


def test_malformed_post_response_surfaces_as_fetch_error():
    """A structurally broken results page (parser-level failure) still surfaces as a
    FetchError through the scraper, not a raw parser exception leaking out."""
    session = FakeSession()
    session.queue_get(FakeResponse(text=TOKEN_PAGE_HTML))
    session.queue_post(FakeResponse(text="<html><body>unexpected page</body></html>"))
    scraper = make_scraper(session)

    with pytest.raises(FetchError, match="no <main"):
        scraper.fetch_course(COURSE)
