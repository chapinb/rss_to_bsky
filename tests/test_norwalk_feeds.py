"""Characterization tests for feed -> post generation using saved real feeds."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
from atproto import models

from rss_post import norwalk_civic_clerk
from rss_post.dedup import PostRef, SeenPosts, keys_in_record
from rss_post.norwalk_feeds import CITY_SEAL, NorwalkFeeds, publish
from rss_post.read_rss import read_rss_items
from tests.conftest import FIXTURES
from tests.test_norwalk_civic_clerk import EVENTS, fake_client

EST = timezone(timedelta(hours=-5))
EASTERN = ZoneInfo("America/New_York")


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
    assert housing.build().build_text() == "Housing Authority Special Meeting"


def test_future_dated_city_items_are_posted_once(freeze_feeds_now):
    """The city feed labels EDT times as -0500, so in summer new items look an
    hour in the future and pass two consecutive hourly windows. The second run
    skips them because they are already posted.
    """
    client = MagicMock()
    seen = SeenPosts()
    future = "https://www.norwalkct.gov/Calendar.aspx?EID=17281"

    for now in (
        datetime(2026, 11, 5, 12, 0, tzinfo=EST),
        datetime(2026, 11, 5, 13, 0, tzinfo=EST),
    ):
        freeze_feeds_now(now)
        with feeds_from_fixture("norwalk_calendar.xml"):
            feeds = NorwalkFeeds(timedelta(hours=24))
            publish(client, [("calendar", feeds.get_committee_events)], seen)

    posted = [c.args[0].link for c in client.post.call_args_list]
    assert posted.count(future) == 1
    assert len(posted) == len(set(posted)) == 3


def test_city_calendar_card_description_is_plain_text(freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 5, 12, 0, tzinfo=EST))
    with feeds_from_fixture("norwalk_calendar.xml"):
        posts = NorwalkFeeds(timedelta(hours=24)).get_committee_events()

    housing = next(p for p in posts if card(p).uri.endswith("EID=17270"))
    assert card(housing).description == (
        "Event date: November 18, 2026 Event Time: 05:30 PM - 06:30 PM "
        "Location: Norwalk, CT 06851-5125"
    )
    assert "<" not in card(housing).description


def test_ct_mirror_only_posts_stories_mentioning_norwalk(freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 12, 0, 0, tzinfo=UTC))
    with feeds_from_fixture("ctmirror.xml"):
        posts = NorwalkFeeds(timedelta(days=7)).get_ct_mirror_stories()

    # The fixture has three recent stories; only one mentions Norwalk, and
    # only in its full content (not the title or summary).
    assert [card(p).title for p in posts] == [
        "Sen. Rob Sampson to lead smallest GOP caucus in CT history"
    ]


def test_ct_mirror_respects_posting_window(freeze_feeds_now):
    freeze_feeds_now(datetime(2026, 11, 12, 0, 0, tzinfo=UTC))
    with feeds_from_fixture("ctmirror.xml"):
        posts = NorwalkFeeds(timedelta(hours=1)).get_ct_mirror_stories()

    assert posts == []


def meeting_posts(meeting: dict, now: datetime):
    with patch(
        "rss_post.norwalk_feeds.norwalk_civic_clerk.get_todays_meetings",
        return_value=[meeting],
    ):
        return NorwalkFeeds(timedelta(hours=1)).get_todays_norwalk_meetings(now)


def test_meeting_posts_have_an_agenda_card_and_zoom_link():
    meeting = {
        "title": "City Council",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/1049/files",
        "when": "Nov 10, 2026 at 7:30 PM EST",
        "starts_at": datetime(2026, 11, 10, 19, 30, tzinfo=EASTERN),
        "location": "Council Chambers, 125 East Avenue, Norwalk, CT 06851",
        "zoom": "https://norwalkct-org.zoom.us/j/89654026934",
    }
    [post] = meeting_posts(meeting, datetime(2026, 11, 10, 7, tzinfo=EASTERN))

    builder = post.build()
    assert builder.build_text() == (
        "City Council\nNov 10, 2026 at 7:30 PM EST\nJoin on Zoom"
    )
    assert [f.features[0].uri for f in builder.build_facets()] == [meeting["zoom"]]
    assert card(post).uri == meeting["agenda"]
    assert card(post).title == "City Council"
    assert card(post).description == (
        "Nov 10, 2026 at 7:30 PM EST · "
        "Council Chambers, 125 East Avenue, Norwalk, CT 06851"
    )
    assert post.thumb_url == CITY_SEAL
    # Still keyed by the agenda link and the day, not just the card's link.
    assert SeenPosts.key(post) == (
        "https://norwalkct.portal.civicclerk.com/event/1049/files on 2026-11-10"
    )


def test_meeting_without_zoom_link_or_location_omits_them():
    meeting = {
        "title": "Defined Contribution (CANCELLED)",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/1387/files",
        "when": "Nov 11, 2026 at 6:45 PM EST",
        "starts_at": datetime(2026, 11, 11, 18, 45, tzinfo=EASTERN),
        "location": None,
        "zoom": None,
    }
    [post] = meeting_posts(meeting, datetime(2026, 11, 11, 7, tzinfo=EASTERN))

    builder = post.build()
    assert builder.build_text() == (
        "Defined Contribution (CANCELLED)\nNov 11, 2026 at 6:45 PM EST"
    )
    assert builder.build_facets() == []
    assert card(post).description == "Nov 11, 2026 at 6:45 PM EST"
    assert SeenPosts.key(post) == (
        "https://norwalkct.portal.civicclerk.com/event/1387/files cancelled"
        " on 2026-11-11"
    )


def test_meeting_posted_earlier_today_as_text_is_not_posted_again_as_a_card():
    # Before agenda cards, the agenda was a link in the post's text.
    agenda = "https://norwalkct.portal.civicclerk.com/event/984/files"
    now = datetime(2026, 11, 12, 9, tzinfo=EASTERN)
    old_text = "City of Norwalk CT Meetings\nFinance & Claims Committee\nAgenda\n"
    record = models.AppBskyFeedPost.Record(
        text=old_text,
        created_at=datetime(2026, 11, 12, 12, tzinfo=UTC).isoformat(),
        facets=[
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=len(old_text) - 7, byte_end=len(old_text)
                ),
                features=[models.AppBskyRichtextFacet.Link(uri=agenda)],
            )
        ],
    )
    meeting = {
        "title": "Finance & Claims Committee",
        "agenda": agenda,
        "when": "Nov 12, 2026 at 6:00 PM EST",
        "starts_at": datetime(2026, 11, 12, 18, tzinfo=EASTERN),
        "location": "Virtual via Zoom",
        "zoom": None,
    }
    [post] = meeting_posts(meeting, now)

    assert not SeenPosts(keys_in_record(record, now)).is_new(post)


class MeetingRuns:
    """Hourly runs of the meetings source against saved CivicClerk data.

    What each run posts is turned back into Bluesky records and read the way
    the next run reads the account's history, so the keys round-trip.
    """

    def __init__(self):
        self.events = EVENTS
        self.history: dict[str, PostRef] = {}
        self.sent: list = []

    def run(
        self, now: datetime, civic_clerk_up: bool = True, reminders: bool = False
    ) -> list[str]:
        def handler(request):
            if not civic_clerk_up:
                return httpx.Response(503)
            return httpx.Response(200, json=self.events)

        def send(post):
            ref = PostRef(f"at://p/{len(self.sent) + 1}", "c")
            self.sent.append((post, ref))
            return ref

        client = MagicMock()
        client.post.side_effect = send
        already_sent = len(self.sent)
        feeds = NorwalkFeeds(timedelta(hours=1))
        sources = [("meetings", lambda: feeds.get_todays_norwalk_meetings(now))]
        if reminders:
            sources.append(("reminders", lambda: feeds.get_meeting_reminders(now)))
        with (
            patch.object(
                norwalk_civic_clerk,
                "CivicClerkClient",
                side_effect=lambda _tenant: fake_client(handler),
            ),
            patch.object(norwalk_civic_clerk.time, "sleep"),
        ):
            publish(client, sources, SeenPosts(self.history))
        sent = self.sent[already_sent:]
        for post, ref in sent:
            builder = post.build()
            record = models.AppBskyFeedPost.Record(
                text=builder.build_text(),
                facets=builder.build_facets(),
                embed=post.embed,
                created_at=now.astimezone(UTC).isoformat(),
                reply=post.reply,
            )
            for key in keys_in_record(record, now):
                self.history[key] = ref
        # A meeting by its card's title; a reply (reminder) by its text.
        return [
            card(post).title if post.embed else post.build().build_text()
            for post, _ in sent
        ]


# The saved meetings, all in the evening: four on Nov 11 (6:00-7:00 PM), two
# on Nov 12.
NOV_11_MEETINGS = [
    "Pension Board of Trustees (CANCELLED)",
    "Ad Hoc Sustainability and Resilience Committee (CANCELLED)",
    "Defined Contribution (CANCELLED)",
    "Recreation, Parks & Cultural Affairs Committee (CANCELLED)",
]
NOV_12_MEETINGS = ["Finance & Claims Committee", "Library Board of Directors"]


def test_missed_7am_meetings_are_caught_up_once():
    runs = MeetingRuns()

    assert runs.run(datetime(2026, 11, 11, 7, tzinfo=EASTERN), False) == []
    assert (
        runs.run(datetime(2026, 11, 11, 8, tzinfo=EASTERN))
        == NOV_11_MEETINGS + NOV_12_MEETINGS
    )
    assert runs.run(datetime(2026, 11, 11, 9, tzinfo=EASTERN)) == []
    assert runs.run(datetime(2026, 11, 11, 17, tzinfo=EASTERN)) == []


def test_tomorrows_meetings_are_posted_again_on_the_day():
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 11, 7, tzinfo=EASTERN))

    # Nov 11's meetings have started; Nov 12's goes out again, once.
    assert runs.run(datetime(2026, 11, 12, 7, tzinfo=EASTERN)) == NOV_12_MEETINGS
    assert runs.run(datetime(2026, 11, 12, 8, tzinfo=EASTERN)) == []


def test_meeting_cancelled_later_in_the_day_is_posted_again():
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 11, 7, tzinfo=EASTERN))

    runs.events = {
        **EVENTS,
        "value": [
            {**e, "eventName": e["eventName"] + " (CANCELLED)"}
            if e["eventName"] == "Finance & Claims Committee"
            else e
            for e in EVENTS["value"]
        ],
    }
    assert runs.run(datetime(2026, 11, 11, 12, tzinfo=EASTERN)) == [
        "Finance & Claims Committee (CANCELLED)"
    ]
    assert runs.run(datetime(2026, 11, 11, 13, tzinfo=EASTERN)) == []


def test_meetings_that_have_started_are_not_posted():
    # At 6:30 PM the two 6:00 PM meetings have started.
    runs = MeetingRuns()
    assert (
        runs.run(datetime(2026, 11, 11, 18, 30, tzinfo=EASTERN))
        == NOV_11_MEETINGS[2:] + NOV_12_MEETINGS
    )


FINANCE_REMINDER = "Starting at 7:00 PM — Zoom: norwalkct-org.zoom.us/j/83292194046"
LIBRARY_REMINDER = "Starting at 7:00 PM — Zoom: norwalkct-org.zoom.us/j/84110778128"


def test_reminder_replies_to_the_mornings_meeting_post_once():
    runs = MeetingRuns()
    assert runs.run(datetime(2026, 11, 12, 7, 5, tzinfo=EASTERN), reminders=True) == (
        NOV_12_MEETINGS
    )
    # Hourly runs: nothing until the first within 75 minutes of the 7 PM start.
    for hour in range(8, 18):
        assert (
            runs.run(datetime(2026, 11, 12, hour, 5, tzinfo=EASTERN), True, True) == []
        )
    assert runs.run(datetime(2026, 11, 12, 18, 5, tzinfo=EASTERN), True, True) == [
        FINANCE_REMINDER,
        LIBRARY_REMINDER,
    ]
    assert runs.run(datetime(2026, 11, 12, 18, 55, tzinfo=EASTERN), True, True) == []

    roots = {card(post).title: ref for post, ref in runs.sent if post.reply is None}
    reminders = [post for post, _ in runs.sent if post.reply is not None]
    assert [r.reply.root.uri for r in reminders] == [
        roots["Finance & Claims Committee"].uri,
        roots["Library Board of Directors"].uri,
    ]
    assert all(r.reply.parent.uri == r.reply.root.uri for r in reminders)


def test_reminder_window_tolerates_a_late_hourly_run():
    """Runs at 5:58 and 7:02 PM: the 5:58 run is the only one before 7 PM."""
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 12, 7, tzinfo=EASTERN))
    assert runs.run(datetime(2026, 11, 12, 17, 58, tzinfo=EASTERN), True, True) == [
        FINANCE_REMINDER,
        LIBRARY_REMINDER,
    ]


def test_reminder_replies_to_a_meeting_post_made_in_the_same_run():
    # The 7 AM post was missed; the meeting post and its reminder go out together.
    runs = MeetingRuns()
    assert runs.run(datetime(2026, 11, 12, 18, 5, tzinfo=EASTERN), True, True) == [
        *NOV_12_MEETINGS,
        FINANCE_REMINDER,
        LIBRARY_REMINDER,
    ]
    [(_, finance), _, (reminder, _), _] = runs.sent
    assert reminder.reply.root.uri == finance.uri


def test_reminder_is_skipped_without_todays_meeting_post():
    # Only yesterday's post of the meeting exists.
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 11, 7, tzinfo=EASTERN))
    client = MagicMock()
    meeting = {
        "title": "Finance & Claims Committee",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/984/files",
        "when": "Nov 12, 2026 at 7:00 PM EST",
        "starts_at": datetime(2026, 11, 12, 19, tzinfo=EASTERN),
        "location": None,
        "zoom": None,
    }
    feeds = NorwalkFeeds(timedelta(hours=1))
    now = datetime(2026, 11, 12, 18, 5, tzinfo=EASTERN)
    with patch(
        "rss_post.norwalk_feeds.norwalk_civic_clerk.get_todays_meetings",
        return_value=[meeting],
    ):
        result = publish(
            client,
            [("reminders", lambda: feeds.get_meeting_reminders(now))],
            SeenPosts(runs.history),
        )

    assert result == (0, 0)
    client.post.assert_not_called()


def test_no_reminders_for_cancelled_or_started_meetings():
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 11, 7, tzinfo=EASTERN))
    # Nov 11's meetings are all cancelled; at 6:30 PM two have also started.
    assert runs.run(datetime(2026, 11, 11, 18, 30, tzinfo=EASTERN), True, True) == []


def test_reminder_without_zoom_link_gives_the_time():
    meeting = {
        "title": "Board",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/1/files",
        "when": "Nov 12, 2026 at 9:30 AM EST",
        "starts_at": datetime(2026, 11, 12, 9, 30, tzinfo=EASTERN),
        "location": None,
        "zoom": None,
    }
    with patch(
        "rss_post.norwalk_feeds.norwalk_civic_clerk.get_todays_meetings",
        return_value=[meeting],
    ):
        [post] = NorwalkFeeds(timedelta(hours=1)).get_meeting_reminders(
            datetime(2026, 11, 12, 8, 30, tzinfo=EASTERN)
        )

    assert post.build().build_text() == "Starting at 9:30 AM"
    assert post.reply_kind == "reminder"
    assert post.reply_root == (
        "https://norwalkct.portal.civicclerk.com/event/1/files on 2026-11-12"
    )
