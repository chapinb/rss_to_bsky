"""Norwalk Public Library events, using a saved copy of the real feed.

The fixture is the library's JSON feed as read on 2026-11-12. Events through
2026-11-19 are complete; later ones keep only the fields the filter reads
(descriptions cut short), since they are there so recurring series are
counted as in the whole feed.
"""

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from rss_post import norwalk_library
from rss_post.norwalk_feeds import NorwalkFeeds
from tests.conftest import FIXTURES

EST = timezone(timedelta(hours=-5))
FIXTURE = FIXTURES / "norwalk_library_events.json"


def feed() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def titles(events) -> list[str]:
    return [e.title for e in events]


def workshop(data: list[dict]) -> dict:
    """An adult program on 2026-11-12 that passes every filter."""
    return next(e for e in data if e["id"] == "4017")


@pytest.fixture
def library_site():
    """Serve the saved feed in place of the real calendar."""

    def get(url, **_):
        assert url == norwalk_library.EVENTS_URL
        return httpx.Response(
            200, content=FIXTURE.read_bytes(), request=httpx.Request("GET", url)
        )

    with patch("rss_post.norwalk_oyster.httpx.get", side_effect=get) as mock_get:
        yield mock_get


def test_childrens_programs_and_storytimes_are_skipped():
    # 2026-11-12 also has Baby Storytime, LEGO for Littles, LEGO Club and
    # 3D Design for Teens.
    events = norwalk_library.events_on(feed(), date(2026, 11, 12))

    assert titles(events) == ["Navigating Social Security and Medicare Workshop"]
    workshop = events[0]
    assert workshop.url == (
        "https://norwalkpl.librarycalendar.com/event/"
        "navigating-social-security-and-medicare-workshop-4017"
    )
    assert workshop.when == "5:30 PM – 7:30 PM"
    assert workshop.location == "Full Auditorium, Norwalk Public Library"


def test_recurring_series_are_skipped():
    # The weekly Italian conversation group and video game club are on this
    # day too, as is Social Security Video Connect on most days.
    events = norwalk_library.events_on(feed(), date(2026, 11, 14))

    assert titles(events) == [
        "Crafting for Adults: Autumnal Door Signs",
        "ALGO Swap Meets - AlgoMania",
    ]
    assert all("Scopri" not in t for t in titles(events))


def test_closures_are_not_events():
    data = feed()
    data.append({**workshop(data), "title": "Library Closed", "url": "closed"})
    data.append({**workshop(data), "title": "CANCELLED: Author Talk", "url": "x"})

    events = norwalk_library.events_on(data, date(2026, 11, 12))

    assert titles(events) == ["Navigating Social Security and Medicare Workshop"]


def test_at_most_three_a_day_featured_first_then_in_time_order():
    data = feed()
    day = [e for e in data if e["start_date"].startswith("2026-11-18")]
    authorspeak = next(e for e in day if e["title"].startswith("AuthorSpeak"))
    # An earlier, unfeatured adult event: dropped by the cap, since the
    # three featured events that day come first.
    data.append(
        {
            **authorspeak,
            "title": "Morning Lecture",
            "url": f"{norwalk_library.SITE_URL}/event/morning-lecture-1",
            "start_date": "2026-11-18 09:00:00",
            "end_date": "2026-11-18 10:00:00",
            "featured": False,
        }
    )

    events = norwalk_library.events_on(data, date(2026, 11, 18))

    assert len(events) == norwalk_library.MAX_EVENTS_PER_DAY
    assert "Morning Lecture" not in titles(events)
    assert [e.starts_at for e in events] == sorted(e.starts_at for e in events)
    assert titles(events)[0].startswith("Under the Boardwalk")


def test_events_without_a_start_time_are_skipped():
    data = feed()
    data.append({**workshop(data), "title": "Talk", "start_date": "TBD"})

    assert "Talk" not in titles(norwalk_library.events_on(data, date(2026, 11, 12)))


def test_posts_say_when_and_where_with_a_card(library_site):
    now = datetime(2026, 11, 14, 7, 5, tzinfo=EST)
    posts = NorwalkFeeds(timedelta(hours=1)).get_todays_norwalk_library_events(now)

    assert [p.embed.external.title for p in posts] == [
        "Crafting for Adults: Autumnal Door Signs",
        "ALGO Swap Meets - AlgoMania",
    ]
    craft = posts[0]
    assert craft.build().build_text() == (
        "Today at the library: Crafting for Adults: Autumnal Door Signs "
        "(11:00 AM – 12:00 PM, Studio One, Norwalk Public Library)"
    )
    assert craft.link.startswith("https://norwalkpl.librarycalendar.com/event/")
    assert craft.thumb_url and craft.thumb_url.startswith(norwalk_library.SITE_URL)
    assert "<" not in craft.embed.external.description


def test_events_that_have_started_are_skipped(library_site):
    # The craft starts at 11:00 AM; the swap meet at noon.
    now = datetime(2026, 11, 14, 11, 30, tzinfo=EST)
    posts = NorwalkFeeds(timedelta(hours=1)).get_todays_norwalk_library_events(now)

    assert [p.embed.external.title for p in posts] == ["ALGO Swap Meets - AlgoMania"]


def test_repeat_sessions_on_one_day_get_one_post():
    data = feed()
    first = workshop(data)
    data.append(
        {
            **first,
            "id": "9999",
            "url": f"{norwalk_library.SITE_URL}/event/navigating-9999",
            "start_date": "2026-11-12 19:45:00",
            "end_date": "2026-11-12 21:00:00",
        }
    )

    events = norwalk_library.events_on(data, date(2026, 11, 12))

    assert [e.url for e in events] == [first["url"]]


def test_outside_groups_room_bookings_are_skipped():
    # Bare listings in the real feed: no program type, description or image.
    data = feed()
    bookings = [
        e
        for e in data
        if e["title"] in ("Chamber of Commerce", "Norwalk Aquarium Meeting")
    ]
    assert len(bookings) == 2

    assert norwalk_library.events_on(data, date(2026, 12, 8)) == []
    assert "Norwalk Aquarium Meeting" not in titles(
        norwalk_library.events_on(data, date(2026, 12, 17))
    )


def test_programs_with_only_an_image_or_type_are_not_bookings():
    # The chess tournaments and Access Health CT enrollment have no program
    # type or description, but carry a library-made image.
    data = feed()
    assert titles(norwalk_library.events_on(data, date(2026, 11, 21))) == [
        "Red Bandit Chess Tournament"
    ]
    assert titles(norwalk_library.events_on(data, date(2026, 12, 14))) == [
        "Access Health CT - In-Person Enrollment"
    ]
    typed = {
        **workshop(data),
        "description": "<p>&nbsp;</p>",
        "image": None,
    }
    assert not norwalk_library.is_room_booking(typed, {"Workshop"})
    assert norwalk_library.is_room_booking(typed, set())
