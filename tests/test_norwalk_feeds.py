"""Characterization tests for feed -> post generation using saved real feeds."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from rss_post.norwalk_feeds import NorwalkFeeds
from rss_post.read_rss import read_rss_items
from tests.conftest import FIXTURES

EST = timezone(timedelta(hours=-5))


def feeds_from_fixture(name: str):
    """Patch feed reading so any URL returns the named fixture's items."""
    return patch(
        "rss_post.norwalk_feeds.read_rss_items",
        side_effect=lambda _url: read_rss_items(str(FIXTURES / name)),
    )


def card(post):
    return post.embed.external


def test_city_calendar_posts_recent_items_as_embed_cards(freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 5, 12, 0, tzinfo=EST))
    with feeds_from_fixture("norwalk_calendar.xml"):
        posts = NorwalkFeeds(timedelta(hours=24)).get_committee_events()

    links = [card(p).uri for p in posts]
    assert "https://www.norwalkct.gov/Calendar.aspx?EID=17270" in links
    assert "https://www.norwalkct.gov/Calendar.aspx?EID=17059" in links
    # Older than the window.
    assert "https://www.norwalkct.gov/Calendar.aspx?EID=17265" not in links

    housing = next(p for p in posts if card(p).uri.endswith("EID=17270"))
    assert card(housing).title == "Housing Authority Special Meeting"
    assert housing.build().build_text() == ""


def test_city_calendar_accepts_future_dated_items(freeze_feeds_now):
    """Known bug (see docs/MODERNIZATION.md, Phase 4).

    Items dated after "now" have a negative age and pass the window check.
    The city feed labels EDT times as -0500, so in summer every new item
    looks an hour in the future and is posted on two consecutive runs.
    """
    freeze_feeds_now(datetime(2026, 11, 5, 12, 0, tzinfo=EST))
    with feeds_from_fixture("norwalk_calendar.xml"):
        posts = NorwalkFeeds(timedelta(hours=24)).get_committee_events()

    assert "https://www.norwalkct.gov/Calendar.aspx?EID=17281" in [
        card(p).uri for p in posts
    ]
    assert len(posts) == 3


def test_city_calendar_card_description_is_raw_html(freeze_feeds_now):
    """Current behaviour; HTML stripping for cards is planned in Phase 9."""
    freeze_feeds_now(datetime(2026, 11, 5, 12, 0, tzinfo=EST))
    with feeds_from_fixture("norwalk_calendar.xml"):
        posts = NorwalkFeeds(timedelta(hours=24)).get_committee_events()

    assert "<strong>" in card(posts[0]).description


def test_ct_mirror_only_posts_stories_mentioning_norwalk(freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc))
    with feeds_from_fixture("ctmirror.xml"):
        posts = NorwalkFeeds(timedelta(days=7)).get_ct_mirror_stories()

    # The fixture has three recent stories; only one mentions Norwalk, and
    # only in its full content (not the title or summary).
    assert [card(p).title for p in posts] == [
        "Sen. Rob Sampson to lead smallest GOP caucus in CT history"
    ]


def test_ct_mirror_respects_posting_window(freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc))
    with feeds_from_fixture("ctmirror.xml"):
        posts = NorwalkFeeds(timedelta(hours=1)).get_ct_mirror_stories()

    assert posts == []


def test_meeting_posts_include_title_time_and_links():
    meeting = {
        "title": "City Council",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/1049/files",
        "when": "Nov 10, 2026 at 7:30 PM EST",
        "where": "https://norwalkct-org.zoom.us/j/89654026934",
    }
    with patch(
        "rss_post.norwalk_feeds.norwalk_civic_clerk.get_todays_meetings",
        return_value=[meeting],
    ):
        posts = NorwalkFeeds(timedelta(hours=1)).get_todays_norwalk_meetings()

    builder = posts[0].build()
    text = builder.build_text()
    assert text.startswith("City of Norwalk CT Meetings\nCity Council\n")
    assert "Nov 10, 2026 at 7:30 PM EST" in text
    uris = [f.features[0].uri for f in builder.build_facets()]
    assert uris == [meeting["agenda"], meeting["where"]]
