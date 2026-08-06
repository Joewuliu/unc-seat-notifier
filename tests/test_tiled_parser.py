"""Parser tests against real, sanitized UNC Tiled Search fixtures. No network, no
mocking - parse_tiled_search/extract_csrf_token are pure functions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.tiled_parser import extract_csrf_token, parse_tiled_search

from .helpers import FIXTURES_DIR

OBSERVED_AT = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def load(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


COMP210_HTML = load("unc_class_search_tiled_comp210.html")
SUBJECT_ALL_HTML = load("unc_class_search_tiled_subject_all.html")
ZERO_RESULTS_HTML = load("unc_class_search_tiled_zero_results.html")


# --- parse_tiled_search: comp210 (small, 2-card) fixture -----------------------------


def test_parses_two_sections_from_comp210_fixture():
    snapshots = parse_tiled_search(COMP210_HTML, observed_at=OBSERVED_AT)

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
    snapshots = parse_tiled_search(COMP210_HTML, observed_at=OBSERVED_AT)

    for snapshot in snapshots:
        assert snapshot.waitlist_available is None
        assert snapshot.waitlist_capacity is None
        assert snapshot.waitlist_status_unknown is True
        assert snapshot.waitlist_active is False


# --- parse_tiled_search: subject_all (large, 324-card) fixture ------------------------


def test_parses_all_324_sections_from_large_fixture():
    snapshots = parse_tiled_search(SUBJECT_ALL_HTML, observed_at=OBSERVED_AT)

    assert len(snapshots) == 324
    distinct_courses = {(s.section_key.subject, s.section_key.catalog_number) for s in snapshots}
    assert len(distinct_courses) == 47  # matches the fixture's "Found 47 course(s)"


def test_zero_available_seats_parses_as_closed_status():
    snapshots = parse_tiled_search(SUBJECT_ALL_HTML, observed_at=OBSERVED_AT)

    full_sections = [s for s in snapshots if s.seats_available == 0]
    assert full_sections  # the fixture is known to contain full sections
    for snapshot in full_sections:
        assert snapshot.status == "closed"
        assert snapshot.seats_capacity > 0


def test_alphanumeric_catalog_numbers_stay_strings():
    snapshots = parse_tiled_search(SUBJECT_ALL_HTML, observed_at=OBSERVED_AT)
    catalog_numbers = {s.section_key.catalog_number for s in snapshots}

    for expected in ("89", "89H", "283", "283H"):
        assert expected in catalog_numbers
        matching = next(s for s in snapshots if s.section_key.catalog_number == expected)
        assert isinstance(matching.section_key.catalog_number, str)


# --- parse_tiled_search: zero-results fixture -----------------------------------------


def test_zero_results_page_returns_empty_list_not_an_error():
    snapshots = parse_tiled_search(ZERO_RESULTS_HTML, observed_at=OBSERVED_AT)

    assert snapshots == []


# --- malformed structure --------------------------------------------------------------


def test_missing_main_element_raises_fetch_error():
    with pytest.raises(FetchError, match="no <main"):
        parse_tiled_search("<html><body>nothing here</body></html>", observed_at=OBSERVED_AT)


def test_neither_summary_nor_no_results_message_raises_fetch_error():
    html = '<html><body><main id="main"><p>Something unexpected</p></main></body></html>'

    with pytest.raises(FetchError, match="neither a results summary"):
        parse_tiled_search(html, observed_at=OBSERVED_AT)


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
        parse_tiled_search(html, observed_at=OBSERVED_AT)


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
        parse_tiled_search(html, observed_at=OBSERVED_AT)


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
        parse_tiled_search(html, observed_at=OBSERVED_AT)


# --- extract_csrf_token ----------------------------------------------------------------


def test_extract_csrf_token_from_real_fixture():
    token = extract_csrf_token(COMP210_HTML)

    assert token == "REDACTED_CSRF_TOKEN"


def test_extract_csrf_token_missing_raises_fetch_error():
    with pytest.raises(FetchError, match="csrfmiddlewaretoken"):
        extract_csrf_token("<html><body><form></form></body></html>")
