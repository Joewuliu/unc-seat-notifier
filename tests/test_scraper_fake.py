from __future__ import annotations

import pytest

from unc_notifier.models import CourseKey
from unc_notifier.scraper.base import FetchError
from unc_notifier.scraper.fake import FakeScraper

from .helpers import fixture_course_key, load_fixture_snapshots


def test_fake_scraper_returns_configured_fixture():
    course = fixture_course_key("comp550_available.json")
    scraper = FakeScraper({course: load_fixture_snapshots("comp550_available.json")})

    result = scraper.fetch_course(course)

    assert len(result) == 2
    assert result[0].section_key.section == "001"
    assert result[0].seats_available == 3


def test_fake_scraper_raises_configured_exception():
    course = CourseKey(term="2026 Spring", subject="COMP", catalog_number="999")
    scraper = FakeScraper({course: FetchError("simulated network error")})

    with pytest.raises(FetchError, match="simulated network error"):
        scraper.fetch_course(course)


def test_fake_scraper_raises_on_unconfigured_course():
    scraper = FakeScraper()

    with pytest.raises(FetchError):
        scraper.fetch_course(CourseKey(term="2026 Spring", subject="PHYS", catalog_number="101"))


def test_fake_scraper_records_calls_for_dedup_assertions():
    course = fixture_course_key("comp550_available.json")
    scraper = FakeScraper({course: load_fixture_snapshots("comp550_available.json")})

    scraper.fetch_course(course)
    scraper.fetch_course(course)

    assert scraper.calls == [course, course]


def test_fake_scraper_supports_scripted_sequence_of_responses():
    """A callable response lets a single course flip from full to available across calls,
    which is exactly what the poller-level flap/cooldown tests need."""
    course = fixture_course_key("comp550_full.json")
    responses = iter(
        [
            load_fixture_snapshots("comp550_full.json"),
            load_fixture_snapshots("comp550_available.json"),
        ]
    )
    scraper = FakeScraper({course: lambda: next(responses)})

    first = scraper.fetch_course(course)
    second = scraper.fetch_course(course)

    assert first[0].seats_available == 0
    assert second[0].seats_available == 3
