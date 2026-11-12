"""Norwalk Oyster events and newsletter, using saved copies of the real data."""

import json
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from rss_post import norwalk_oyster
from rss_post.norwalk_feeds import NorwalkFeeds
from tests.conftest import FIXTURES

EST = timezone(timedelta(hours=-5))


def events_data() -> dict:
    return json.loads((FIXTURES / "norwalk_oyster_events.json").read_text())


def sitemap() -> bytes:
    return (FIXTURES / "norwalk_oyster_sitemap.xml").read_bytes()


def response(url: str, status: int = 200, content: bytes = b"") -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("GET", url))


@pytest.fixture
def no_sleep():
    with patch("rss_post.norwalk_oyster.time.sleep"):
        yield


@pytest.fixture
def oyster_site():
    """Serve the saved events and sitemap in place of the real sites."""
    pages = {
        norwalk_oyster.EVENTS_URL: (FIXTURES / "norwalk_oyster_events.json"),
        norwalk_oyster.NEWSLETTER_SITEMAP_URL: (
            FIXTURES / "norwalk_oyster_sitemap.xml"
        ),
    }
    with patch(
        "rss_post.norwalk_oyster.httpx.get",
        side_effect=lambda url, **_: response(url, content=pages[url].read_bytes()),
    ) as mock_get:
        yield mock_get


def test_only_top_rated_events_on_the_day():
    events = norwalk_oyster.events_on(events_data(), date(2026, 11, 7))

    assert [e.name for e in events] == [
        "American Chamber Orchestra - 2026-2027 Season Opener",
        "November 2026 Pro Tour",
    ]
    orchestra = events[0]
    assert orchestra.url.startswith("https://norwalkoyster.com/directory/event/")
    assert orchestra.when == "7:30 PM – 9:30 PM"


def test_duplicate_and_undated_events_are_skipped():
    data = events_data()
    data["events"][2]["recurring"] = "Dupe"
    data["events"].append({**data["events"][3], "date": "TBD"})

    events = norwalk_oyster.events_on(data, date(2026, 11, 7))

    assert [e.name for e in events] == ["November 2026 Pro Tour"]


def test_unpadded_and_padded_dates_both_parse():
    assert norwalk_oyster.parse_day("11/7/2026") == date(2026, 11, 7)
    assert norwalk_oyster.parse_day("06/05/2026") == date(2026, 6, 5)
    assert norwalk_oyster.parse_day("") is None


def test_newsletter_issues_come_from_news_sitemap_entries():
    issues = norwalk_oyster.issues_from_sitemap(sitemap())

    assert [(i.title, i.url, i.published) for i in issues] == [
        (
            "Fifteen kitchens. One goal.",
            "https://newsletter.norwalkoyster.com/p/fifteen-kitchens-one-goal",
            datetime(2026, 11, 11, 14, 0, tzinfo=UTC),
        ),
        (
            "Boots on. No experience needed.",
            "https://newsletter.norwalkoyster.com/p/boots-on-no-experience-needed",
            datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
        ),
    ]


def test_newsletter_posts_issues_within_window(oyster_site, freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 11, 15, 0, tzinfo=UTC))
    posts = NorwalkFeeds(timedelta(hours=24)).get_norwalk_oyster_newsletter()

    assert [p.link for p in posts] == [
        "https://newsletter.norwalkoyster.com/p/fifteen-kitchens-one-goal"
    ]
    assert posts[0].build().build_text() == (
        "Norwalk Oyster: Fifteen kitchens. One goal."
    )
    assert posts[0].embed.external.title == "Fifteen kitchens. One goal."


def test_event_posts_say_when_and_where(oyster_site, freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 7, 7, 5, tzinfo=EST))
    posts = NorwalkFeeds(timedelta(hours=1)).get_todays_norwalk_oyster_events()

    assert len(posts) == 2
    orchestra = posts[0]
    text = orchestra.build().build_text()
    assert text.startswith(
        "Today: American Chamber Orchestra - 2026-2027 Season Opener (7:30 PM – 9:30 PM, "
    )
    assert orchestra.embed.external.title == (
        "American Chamber Orchestra - 2026-2027 Season Opener"
    )
    assert orchestra.link.startswith("https://norwalkoyster.com/directory/event/")


def test_server_errors_are_retried_then_raised(no_sleep):
    url = norwalk_oyster.EVENTS_URL
    with (
        patch(
            "rss_post.norwalk_oyster.httpx.get",
            return_value=response(url, status=503),
        ) as mock_get,
        pytest.raises(httpx.HTTPStatusError),
    ):
        norwalk_oyster.fetch(url)
    assert mock_get.call_count == norwalk_oyster.ATTEMPTS


def test_client_errors_are_not_retried(no_sleep):
    url = norwalk_oyster.NEWSLETTER_SITEMAP_URL
    with (
        patch(
            "rss_post.norwalk_oyster.httpx.get",
            return_value=response(url, status=403),
        ) as mock_get,
        pytest.raises(httpx.HTTPStatusError),
    ):
        norwalk_oyster.fetch(url)
    assert mock_get.call_count == 1


def test_network_error_then_success(no_sleep):
    url = norwalk_oyster.EVENTS_URL
    ok = response(url, content=b"{}")
    with patch(
        "rss_post.norwalk_oyster.httpx.get",
        side_effect=[httpx.ConnectError("down"), ok],
    ):
        assert norwalk_oyster.fetch(url) is ok


def test_events_that_have_started_are_skipped(oyster_site):
    # The Pro Tour starts at 10:00 AM; the orchestra at 7:30 PM.
    now = datetime(2026, 11, 7, 11, 0, tzinfo=EST)
    posts = NorwalkFeeds(timedelta(hours=1)).get_todays_norwalk_oyster_events(now)

    assert [p.embed.external.title for p in posts] == [
        "American Chamber Orchestra - 2026-2027 Season Opener"
    ]


def test_event_times_without_am_pm_are_not_treated_as_started():
    event = norwalk_oyster.Event(
        name="Talk", url="https://example.org", day=date(2026, 11, 7), time="7:00"
    )
    assert event.starts_at is None
    timed = norwalk_oyster.Event(
        name="Talk", url="https://example.org", day=date(2026, 11, 7), time="7:00 PM"
    )
    assert timed.starts_at == datetime(
        2026, 11, 7, 19, 0, tzinfo=norwalk_oyster.EASTERN
    )
