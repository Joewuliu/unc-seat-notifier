"""Pure HTML parsing for UNC's Tiled Search (``/class-search/tiled/``) and the
CSRF token needed to POST to it.

No network calls anywhere in this module - it only turns already-fetched HTML text
into our domain types, or raises FetchError for anything it can't confidently parse.
This keeps parsing logic testable against static fixtures, completely independent of
UncClassSearchScraper's requests.Session/CSRF orchestration.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from unc_notifier.models import SeatSnapshot, SectionKey
from unc_notifier.scraper.base import FetchError

_SEATS_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)\s+available seats", re.IGNORECASE)


def extract_csrf_token(html: str) -> str:
    """Pull csrfmiddlewaretoken's value out of a class-search page's hidden input."""
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", attrs={"name": "csrfmiddlewaretoken"})
    if token_input is None or not token_input.get("value"):
        raise FetchError("could not find csrfmiddlewaretoken in the page")
    return token_input["value"]


def parse_tiled_search(html: str, *, observed_at: datetime) -> list[SeatSnapshot]:
    """Parse a Tiled Search results page into one SeatSnapshot per section card.

    ``observed_at`` is supplied by the caller (never read from a clock here) so this
    function stays pure and deterministic for tests. A legitimate "No results found."
    page returns an empty list - that is a valid, successful result, not an error.
    Anything that doesn't match the expected structure raises FetchError instead of
    returning partial or best-guess data.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main", id="main")
    if main is None:
        raise FetchError("unexpected page structure: no <main id='main'> found")

    jumbotron = main.find("div", class_="jumbotron")
    if jumbotron is not None and "no results found" in jumbotron.get_text(strip=True).lower():
        return []

    summary = main.select_one("p.section__content")
    if summary is None:
        raise FetchError(
            "unexpected page structure: neither a results summary nor a "
            "'no results found' message was present"
        )

    counts = summary.find_all("strong")
    if len(counts) != 2:
        raise FetchError(f"unexpected results summary format: {summary.get_text(strip=True)!r}")
    try:
        expected_courses = int(counts[0].get_text(strip=True))
        expected_sections = int(counts[1].get_text(strip=True))
    except ValueError as exc:
        raise FetchError(f"could not parse results summary counts: {exc}") from exc

    cards = main.find_all("div", class_="card")
    if len(cards) != expected_sections:
        raise FetchError(
            f"results summary declared {expected_sections} section(s) but the page "
            f"contains {len(cards)} card(s)"
        )

    snapshots = [_parse_card(card, observed_at) for card in cards]

    actual_courses = {(s.section_key.subject, s.section_key.catalog_number) for s in snapshots}
    if len(actual_courses) != expected_courses:
        raise FetchError(
            f"results summary declared {expected_courses} course(s) but the page "
            f"contains {len(actual_courses)} distinct course(s)"
        )

    return snapshots


def _parse_card(card: Tag, observed_at: datetime) -> SeatSnapshot:
    header = card.find("h2")
    if header is None:
        raise FetchError("card is missing its <h2> header")
    # .split() with no args collapses any run of whitespace - the real page is
    # inconsistent about how many spaces separate subject/catalog_number (2 or 3
    # observed), so a fixed-width or single-space split would be wrong.
    tokens = header.get_text().split()
    if len(tokens) != 3:
        raise FetchError(
            f"could not parse subject/catalog_number/section from card header "
            f"{header.get_text()!r}"
        )
    subject, catalog_number, section = tokens

    card_body = card.find("div", class_="card-body")
    if card_body is None:
        raise FetchError("card is missing its card-body")
    body_paragraphs = card_body.find_all("p")
    if not body_paragraphs:
        raise FetchError("card-body is missing its term paragraph")
    term = body_paragraphs[0].get_text(strip=True)

    seats_el = card.find("p", class_="card-available-seats")
    if seats_el is None:
        raise FetchError("card is missing its available-seats element")
    match = _SEATS_PATTERN.search(seats_el.get_text())
    if match is None:
        raise FetchError(f"could not parse seat counts from {seats_el.get_text(strip=True)!r}")
    seats_available = int(match.group(1))
    seats_capacity = int(match.group(2))

    return SeatSnapshot(
        section_key=SectionKey(
            term=term, subject=subject, catalog_number=catalog_number, section=section
        ),
        seats_available=seats_available,
        seats_capacity=seats_capacity,
        # Confirmed absent from every public UNC view checked (Standard, Advanced,
        # Tiled search) - never fabricate 0/0 here, that would falsely claim "no
        # waitlist" when the truth is "this data source doesn't say."
        waitlist_available=None,
        waitlist_capacity=None,
        status="open" if seats_available > 0 else "closed",
        observed_at=observed_at,
    )
