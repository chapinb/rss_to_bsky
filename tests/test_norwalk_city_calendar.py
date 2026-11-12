"""City of Norwalk community calendars, using saved copies of the real feeds.

``norwalk_rec_parks_calendar.xml`` and ``norwalk_main_city_calendar.xml`` are
the Recreation & Parks and Main City Calendar feeds; ``norwalk_all_calendars``
is the all-categories feed, which has many events on one day.
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from rss_post import norwalk_city_calendar as city
from rss_post.norwalk_feeds import NorwalkFeeds
from rss_post.norwalk_oyster import Event as OysterEvent
from tests.conftest import FIXTURES

EST = timezone(timedelta(hours=-5))

REC_PARKS = FIXTURES / "norwalk_rec_parks_calendar.xml"
MAIN_CITY = FIXTURES / "norwalk_main_city_calendar.xml"
ALL_CALENDARS = FIXTURES / "norwalk_all_calendars.xml"
ALL_CALENDARS_URL = city.FEED_URL.format("All-calendar.xml")


def response(url: str, content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, request=httpx.Request("GET", url))


@pytest.fixture
def city_site():
    """Serve the saved feeds in place of the city's site."""
    pages = {
        city.REC_PARKS_URL: REC_PARKS,
        city.MAIN_CITY_URL: MAIN_CITY,
        ALL_CALENDARS_URL: ALL_CALENDARS,
    }
    with patch(
        "rss_post.norwalk_oyster.httpx.get",
        side_effect=lambda url, **_: response(url, pages[url].read_bytes()),
    ) as mock_get:
        yield mock_get


@pytest.fixture
def oyster_events():
    """Norwalk Oyster's top-rated events for the day (none unless set)."""
    with patch(
        "rss_post.norwalk_feeds.norwalk_oyster.get_events_on", return_value=[]
    ) as mock_events:
        yield mock_events


def todays_posts(feed_url: str, now: datetime, feeds: NorwalkFeeds | None = None):
    feeds = feeds or NorwalkFeeds(timedelta(hours=1))
    return feeds.get_todays_city_calendar_events("City of Norwalk", feed_url, now)


def test_event_date_time_place_and_image_are_read():
    [event] = city.events_from_feed(REC_PARKS.read_bytes())

    assert event == city.Event(
        name="Thanksgiving 101: Holiday Prep Workshop",
        url="https://www.norwalkct.gov/Calendar.aspx?EID=17222",
        day=date(2026, 11, 14),
        start=time(10, 0),
        end=time(12, 30),
        place="328 Flax hill rd.",
        image="https://www.norwalkct.gov/DocumentCenter/View/42835",
    )
    assert event.when == "10:00 AM – 12:30 PM"
    assert event.starts_at == datetime(2026, 11, 14, 10, 0, tzinfo=city.EASTERN)


def test_multi_line_place_drops_the_city_line():
    events = city.events_from_feed(MAIN_CITY.read_bytes())

    assert [e.place for e in events] == [
        "Lockwood-Mathews Mansion",
        "Community Room, 125 East Avenue",
        "Council Chambers, 125 East Avenue",
        "Odeens at Oak Hills Park, 165 Fillow Street",
    ]
    assert events[0].image is None


def test_date_ranges_start_on_their_first_day_and_all_day_has_no_time():
    exhibit = city.events_from_feed(MAIN_CITY.read_bytes())[0]

    assert exhibit.name == "Lockwood-Mathews Mansion Museum New Exhibit"
    assert exhibit.day == date(2026, 10, 7)
    assert (exhibit.start, exhibit.end, exhibit.when) == (None, None, "")
    assert exhibit.starts_at is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07:00 PM - 11:59 PM", (time(19, 0), None)),
        ("12:00 AM - 11:59 PM", (None, None)),
        ("01:00 PM - 02:30 PM", (time(13, 0), time(14, 30))),
        ("", (None, None)),
        ("TBD", (None, None)),
    ],
)
def test_parse_times(text, expected):
    assert city.parse_times(text) == expected


def test_events_on_a_day_are_sorted_by_start_time():
    events = city.events_on(ALL_CALENDARS.read_bytes(), date(2026, 11, 18))

    assert [e.name for e in events][:3] == [
        "Meet the Mayor at Odeens",
        "Housing Authority Special Meeting",
        "Harbor Management Commission",
    ]
    assert len(events) == 8


def test_todays_event_is_posted_as_a_card(city_site, oyster_events):
    posts = todays_posts(city.REC_PARKS_URL, datetime(2026, 11, 14, 7, 5, tzinfo=EST))

    [post] = posts
    assert post.build().build_text() == (
        "Today: Thanksgiving 101: Holiday Prep Workshop "
        "(10:00 AM – 12:30 PM, 328 Flax hill rd.)"
    )
    assert post.link == "https://www.norwalkct.gov/Calendar.aspx?EID=17222"
    assert post.thumb_url == "https://www.norwalkct.gov/DocumentCenter/View/42835"
    assert post.embed.external.description == (
        "City of Norwalk · 10:00 AM – 12:30 PM · 328 Flax hill rd."
    )


def test_other_days_events_are_not_posted(city_site, oyster_events):
    assert todays_posts(city.REC_PARKS_URL, datetime(2026, 11, 13, 9, tzinfo=EST)) == []


def test_events_that_have_started_are_skipped(city_site, oyster_events):
    now = datetime(2026, 11, 14, 10, 30, tzinfo=EST)

    assert todays_posts(city.REC_PARKS_URL, now) == []


def test_at_most_two_events_a_day_and_the_same_two_all_day(city_site, oyster_events):
    morning = todays_posts(ALL_CALENDARS_URL, datetime(2026, 11, 18, 7, tzinfo=EST))
    evening = todays_posts(
        ALL_CALENDARS_URL, datetime(2026, 11, 18, 17, 15, tzinfo=EST)
    )

    assert [p.link for p in morning] == [
        "https://www.norwalkct.gov/Calendar.aspx?EID=17269",
        "https://www.norwalkct.gov/Calendar.aspx?EID=17270",
    ]
    # Meet the Mayor (5 PM) has started; the cap doesn't move on to a third.
    assert [p.link for p in evening] == [morning[1].link]


def test_events_norwalk_oyster_posts_are_left_to_it(city_site, oyster_events):
    oyster_events.return_value = [
        OysterEvent(
            name="Meet the Mayor",
            url="https://norwalkoyster.com/directory/event/meet-the-mayor",
            day=date(2026, 11, 18),
        )
    ]
    feeds = NorwalkFeeds(timedelta(hours=1))
    now = datetime(2026, 11, 18, 7, tzinfo=EST)

    posts = todays_posts(ALL_CALENDARS_URL, now, feeds)
    todays_posts(city.MAIN_CITY_URL, now, feeds)

    assert "Meet the Mayor at Odeens" not in [p.embed.external.title for p in posts]
    assert len(posts) == 2
    oyster_events.assert_called_once_with(date(2026, 11, 18))


def test_unreadable_norwalk_oyster_does_not_stop_the_calendar(city_site, oyster_events):
    oyster_events.side_effect = httpx.ConnectError("down")

    posts = todays_posts(city.REC_PARKS_URL, datetime(2026, 11, 14, 7, tzinfo=EST))

    assert len(posts) == 1


@pytest.mark.parametrize(
    ("name", "other", "expected"),
    [
        ("Meet the Mayor at Odeens", "Meet the Mayor", True),
        ("Summer Concert: The Band", "summer concert - the band", True),
        ("Open Jam", "Norwalk Tuesday Night Open Jam", False),
        ("City Council", "Traffic Authority", False),
    ],
)
def test_same_event(name, other, expected):
    assert city.same_event(name, other) is expected
