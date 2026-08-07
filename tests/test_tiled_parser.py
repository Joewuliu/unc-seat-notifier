"""Parser tests against real, sanitized UNC Tiled Search fixtures. No network, no
mocking - parse_tiled_search/extract_csrf_token are pure functions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from unc_notifier.models import CourseKey
from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.tiled_parser import extract_csrf_token, parse_tiled_search

from .helpers import FIXTURES_DIR

OBSERVED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

COMP210_KEY = CourseKey(term="2026 Fall", subject="COMP", catalog_number="210")


def load(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


COMP210_HTML = load("unc_class_search_tiled_comp210.html")
SUBJECT_ALL_HTML = load("unc_class_search_tiled_subject_all.html")
ZERO_RESULTS_HTML = load("unc_class_search_tiled_zero_results.html")
MATH231_WITH_231L_HTML = load("unc_class_search_tiled_math231_with_231l.html")
MATH231_ONLY_RELATED_HTML = load("unc_class_search_tiled_math231_only_related.html")


# --- parse_tiled_search: comp210 (small, 2-card) fixture -----------------------------


def test_parses_two_sections_from_comp210_fixture():
    snapshots = parse_tiled_search(COMP210_HTML, observed_at=OBSERVED_AT, course_key=COMP210_KEY)

    assert len(snapshots) == 2
    first, second = snapshots

    assert first.section_key.term == "2026 Fall"
    assert first.section_key.subject == "COMP"
    assert first.section_key.catalog_number == "210"
    assert first.section_key.section == "001"
    assert first.seats_available == 82
    assert first.seats_capacity == 150
    assert first.status == "open"
    assert first.observed_at == OBSERVED_AT

    assert second.section_key.section == "002"
    assert second.seats_available == 88
    assert second.seats_capacity == 150


def test_waitlist_fields_are_always_none():
    snapshots = parse_tiled_search(COMP210_HTML, observed_at=OBSERVED_AT, course_key=COMP210_KEY)

    for snapshot in snapshots:
        assert snapshot.waitlist_available is None
        assert snapshot.waitlist_capacity is None
        assert snapshot.waitlist_status_unknown is True
        assert snapshot.waitlist_active is False


# --- parse_tiled_search: subject_all (large, 324-card, 47-course) fixture -------------


def test_filters_to_only_the_exact_requested_course_from_a_large_multi_course_page():
    """The declared "Found 47 course(s), 324 section(s)" summary is validated
    against the whole page internally (see the malformed-structure tests below for
    proof that validation still runs), but only the exactly-requested course's cards
    are ever returned."""
    snapshots = parse_tiled_search(
        SUBJECT_ALL_HTML, observed_at=OBSERVED_AT, course_key=COMP210_KEY
    )

    assert len(snapshots) == 2
    assert {s.section_key.section for s in snapshots} == {"001", "002"}
    assert all(s.section_key.catalog_number == "210" for s in snapshots)


def test_exact_catalog_number_excludes_a_related_alphanumeric_variant():
    """Real regression case: UNC's Tiled Search can return a related-but-distinct
    catalog number (here "89H") alongside an exact "89" request, and vice versa -
    both must be filtered to only the exact match, never a substring/prefix match."""
    only_89 = parse_tiled_search(
        SUBJECT_ALL_HTML, observed_at=OBSERVED_AT,
        course_key=CourseKey(term="2026 Fall", subject="COMP", catalog_number="89"),
    )
    only_89h = parse_tiled_search(
        SUBJECT_ALL_HTML, observed_at=OBSERVED_AT,
        course_key=CourseKey(term="2026 Fall", subject="COMP", catalog_number="89H"),
    )

    assert [s.section_key.catalog_number for s in only_89] == ["89"]
    assert [s.section_key.catalog_number for s in only_89h] == ["89H"]
    assert only_89[0].section_key.section == "144"
    assert only_89h[0].section_key.section == "063"

    # Both known from the real fixture to be full sections - proves status="closed"
    # still comes through correctly once filtering is applied.
    assert only_89[0].status == "closed"
    assert only_89h[0].status == "closed"


def test_exact_catalog_number_excludes_a_related_h_suffixed_variant():
    only_283 = parse_tiled_search(
        SUBJECT_ALL_HTML, observed_at=OBSERVED_AT,
        course_key=CourseKey(term="2026 Fall", subject="COMP", catalog_number="283"),
    )
    only_283h = parse_tiled_search(
        SUBJECT_ALL_HTML, observed_at=OBSERVED_AT,
        course_key=CourseKey(term="2026 Fall", subject="COMP", catalog_number="283H"),
    )

    assert [s.section_key.catalog_number for s in only_283] == ["283"]
    assert [s.section_key.catalog_number for s in only_283h] == ["283H"]


def test_no_exact_match_on_a_valid_large_page_returns_empty_list():
    snapshots = parse_tiled_search(
        SUBJECT_ALL_HTML, observed_at=OBSERVED_AT,
        course_key=CourseKey(term="2026 Fall", subject="COMP", catalog_number="999"),
    )

    assert snapshots == []


# --- parse_tiled_search: zero-results fixture -----------------------------------------


def test_zero_results_page_returns_empty_list_not_an_error():
    snapshots = parse_tiled_search(
        ZERO_RESULTS_HTML, observed_at=OBSERVED_AT, course_key=COMP210_KEY
    )

    assert snapshots == []


# --- parse_tiled_search: MATH 231 / MATH 231L regression -----------------------------


MATH231_KEY = CourseKey(term="2027 Spring", subject="MATH", catalog_number="231")


def test_math231_search_result_containing_math231l_keeps_only_the_exact_231_card():
    snapshots = parse_tiled_search(
        MATH231_WITH_231L_HTML, observed_at=OBSERVED_AT, course_key=MATH231_KEY
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.section_key.subject == "MATH"
    assert snapshot.section_key.catalog_number == "231"
    assert snapshot.section_key.section == "001"
    assert snapshot.section_key.term == "2027 Spring"
    assert snapshot.seats_available == 12
    assert snapshot.seats_capacity == 180


def test_math231l_card_is_not_returned_when_requesting_exact_math231():
    snapshots = parse_tiled_search(
        MATH231_WITH_231L_HTML, observed_at=OBSERVED_AT, course_key=MATH231_KEY
    )

    assert all(s.section_key.catalog_number != "231L" for s in snapshots)


def test_math231l_search_result_returns_only_the_231l_card():
    """The filter works symmetrically - requesting 231L exactly must not pull in the
    unrelated 231 card either."""
    math231l_key = CourseKey(term="2027 Spring", subject="MATH", catalog_number="231L")

    snapshots = parse_tiled_search(
        MATH231_WITH_231L_HTML, observed_at=OBSERVED_AT, course_key=math231l_key
    )

    assert len(snapshots) == 1
    assert snapshots[0].section_key.catalog_number == "231L"
    assert snapshots[0].section_key.section == "401"


def test_valid_page_with_only_a_related_course_card_returns_empty_result():
    """A structurally valid page whose only card is the related MATH 231L (no exact
    MATH 231 card at all) must return [] - a successful, empty result - not raise."""
    snapshots = parse_tiled_search(
        MATH231_ONLY_RELATED_HTML, observed_at=OBSERVED_AT, course_key=MATH231_KEY
    )

    assert snapshots == []


# --- malformed structure --------------------------------------------------------------


def test_missing_main_element_raises_fetch_error():
    with pytest.raises(FetchError, match="no <main"):
        parse_tiled_search(
            "<html><body>nothing here</body></html>",
            observed_at=OBSERVED_AT, course_key=COMP210_KEY,
        )


def test_neither_summary_nor_no_results_message_raises_fetch_error():
    html = '<html><body><main id="main"><p>Something unexpected</p></main></body></html>'

    with pytest.raises(FetchError, match="neither a results summary"):
        parse_tiled_search(html, observed_at=OBSERVED_AT, course_key=COMP210_KEY)


def test_card_count_mismatch_against_declared_summary_raises_fetch_error():
    """Summary claims 2 sections but only 1 card is present - a broken/truncated render."""
    html = """
    <html><body><main id="main">
      <p class="section__content small mb-0">
        <span>Found <strong>1</strong> course(s), <strong>2</strong> section(s)</span>
      </p>
      <div class="card text-center">
        <div class="card-header">
          <h2>COMP  210 001</h2>
          <p class="card-available-seats">82/150 available seats</p>
        </div>
        <div class="card-body p-2"><p>2026 Fall</p></div>
      </div>
    </main></body></html>
    """

    with pytest.raises(FetchError, match="declared 2 section"):
        parse_tiled_search(html, observed_at=OBSERVED_AT, course_key=COMP210_KEY)


def test_unparseable_card_header_raises_fetch_error():
    html = """
    <html><body><main id="main">
      <p class="section__content small mb-0">
        <span>Found <strong>1</strong> course(s), <strong>1</strong> section(s)</span>
      </p>
      <div class="card text-center">
        <div class="card-header">
          <h2>COMP 210</h2>
          <p class="card-available-seats">82/150 available seats</p>
        </div>
        <div class="card-body p-2"><p>2026 Fall</p></div>
      </div>
    </main></body></html>
    """

    with pytest.raises(FetchError, match="could not parse subject/catalog_number/section"):
        parse_tiled_search(html, observed_at=OBSERVED_AT, course_key=COMP210_KEY)


def test_unparseable_seat_text_raises_fetch_error():
    html = """
    <html><body><main id="main">
      <p class="section__content small mb-0">
        <span>Found <strong>1</strong> course(s), <strong>1</strong> section(s)</span>
      </p>
      <div class="card text-center">
        <div class="card-header">
          <h2>COMP  210 001</h2>
          <p class="card-available-seats">Call for availability</p>
        </div>
        <div class="card-body p-2"><p>2026 Fall</p></div>
      </div>
    </main></body></html>
    """

    with pytest.raises(FetchError, match="could not parse seat counts"):
        parse_tiled_search(html, observed_at=OBSERVED_AT, course_key=COMP210_KEY)


def test_malformed_unrelated_card_still_fails_the_whole_fetch():
    """Requirement: unrelated (non-matching) cards are filtered out, not treated as
    fatal - but that's only true for well-formed unrelated cards. A structurally
    malformed card still fails the whole fetch even though it belongs to a course
    (MATH 231L) that isn't the one being requested (MATH 231) - a malformed card
    anywhere signals something is wrong with the page itself, not just with a result
    we don't care about."""
    html = """
    <html><body><main id="main">
      <p class="section__content small mb-0">
        <span>Found <strong>2</strong> course(s), <strong>2</strong> section(s)</span>
      </p>
      <div class="card text-center">
        <div class="card-header">
          <h2>MATH  231 001</h2>
          <p class="card-available-seats">12/180 available seats</p>
        </div>
        <div class="card-body p-2"><p>2027 Spring</p></div>
      </div>
      <div class="card text-center">
        <div class="card-header">
          <h2>MATH 231L</h2>
          <p class="card-available-seats">5/24 available seats</p>
        </div>
        <div class="card-body p-2"><p>2027 Spring</p></div>
      </div>
    </main></body></html>
    """

    with pytest.raises(FetchError, match="could not parse subject/catalog_number/section"):
        parse_tiled_search(html, observed_at=OBSERVED_AT, course_key=MATH231_KEY)


# --- extract_csrf_token ----------------------------------------------------------------


def test_extract_csrf_token_from_real_fixture():
    token = extract_csrf_token(COMP210_HTML)

    assert token == "REDACTED_CSRF_TOKEN"


def test_extract_csrf_token_missing_raises_fetch_error():
    with pytest.raises(FetchError, match="csrfmiddlewaretoken"):
        extract_csrf_token("<html><body><form></form></body></html>")
