"""Characterization tests for feed -> post generation using saved real feeds."""

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
from atproto import models

from rss_post import norwalk_civic_clerk, summarize
from rss_post.dedup import PostRef, SeenPosts, keys_in_record
from rss_post.norwalk_civic_clerk import Participation
from rss_post.norwalk_feeds import (
    CITY_SEAL,
    NorwalkFeeds,
    bullets,
    participation_note,
    publish,
)
from rss_post.post import Post
from rss_post.read_rss import read_rss_items
from tests.conftest import FIXTURES
from tests.test_norwalk_civic_clerk import EVENTS, fake_client, saved_agenda

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
        # Agendas are served from the saved ones; set False to fail them.
        self.agendas_up = True
        self.agenda_requests: list[str] = []
        # Agendas served instead of the saved ones, by id.
        self.agendas: dict[str, dict] = {}
        self.history: dict[str, PostRef] = {}
        self.sent: list = []
        # Stands in for the LLM (summarize.summarize) in runs with summaries,
        # unless ``llm`` is set: then the real client runs with LLM_API_KEY set
        # against ``llm``, an httpx handler for the provider's API.
        self.summarizer = MagicMock(return_value="- Item one\n- Item two")
        self.llm = None
        # (posts sent, failures) of the last run's ``publish``.
        self.result: tuple[int, int] | None = None

    def run(
        self,
        now: datetime,
        civic_clerk_up: bool = True,
        reminders: bool = False,
        summaries: bool = False,
    ) -> list[str]:
        def handler(request):
            if not civic_clerk_up:
                return httpx.Response(503)
            if request.url.path.startswith("/v1/Meetings/"):
                agenda_id = request.url.path.rsplit("/", 1)[1]
                self.agenda_requests.append(agenda_id)
                if not self.agendas_up:
                    return httpx.Response(503)
                agenda = self.agendas.get(agenda_id) or saved_agenda(agenda_id)
                return httpx.Response(200, json=agenda)
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
        seen = SeenPosts(self.history)
        if summaries:
            sources.append(("summaries", lambda: feeds.get_agenda_summaries(seen, now)))
        if self.llm is None:
            llm = [
                patch.object(summarize, "configured", return_value=True),
                patch.object(summarize, "enabled", return_value=True),
                patch.object(summarize, "summarize", self.summarizer),
            ]
        else:
            transport = httpx.MockTransport(self.llm)
            llm = [
                patch.dict("os.environ", {"LLM_API_KEY": "test-key"}),
                patch.object(
                    summarize,
                    "_new_client",
                    side_effect=lambda: httpx.Client(transport=transport),
                ),
            ]
        summarize.reset()  # Each run is a new process.
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    norwalk_civic_clerk,
                    "CivicClerkClient",
                    side_effect=lambda _tenant: fake_client(handler),
                )
            )
            stack.enter_context(patch.object(norwalk_civic_clerk.time, "sleep"))
            for patcher in llm:
                stack.enter_context(patcher)
            self.result = publish(client, sources, seen)
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
        # A meeting by its card's title; a reply (reminder, summary) by its text.
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


def test_meeting_posts_note_public_comment_from_the_agenda():
    runs = MeetingRuns()
    assert runs.run(datetime(2026, 11, 12, 7, tzinfo=EASTERN)) == NOV_12_MEETINGS

    # Both saved agendas have a PUBLIC PARTICIPATION section.
    assert [post.build().build_text() for post, _ in runs.sent] == [
        (
            "Finance & Claims Committee\nNov 12, 2026 at 7:00 PM EST\n"
            "🗣 Public comment on the agenda\nJoin on Zoom"
        ),
        (
            "Library Board of Directors\nNov 12, 2026 at 7:00 PM EST\n"
            "🗣 Public comment on the agenda\nJoin on Zoom"
        ),
    ]
    # Read once each, with the client that read the meetings.
    assert runs.agenda_requests == ["8805", "8841"]


def test_agenda_failure_does_not_drop_the_meeting_post():
    runs = MeetingRuns()
    runs.agendas_up = False

    assert runs.run(datetime(2026, 11, 12, 7, tzinfo=EASTERN)) == NOV_12_MEETINGS
    assert [post.build().build_text() for post, _ in runs.sent] == [
        "Finance & Claims Committee\nNov 12, 2026 at 7:00 PM EST\nJoin on Zoom",
        "Library Board of Directors\nNov 12, 2026 at 7:00 PM EST\nJoin on Zoom",
    ]
    # Retried like the meetings, then given up on.
    assert runs.agenda_requests == ["8805"] * 3 + ["8841"] * 3


def test_agendas_are_read_only_for_meetings_still_to_be_posted():
    runs = MeetingRuns()
    # Nov 11's meetings are cancelled and have no agenda; reminders don't
    # read agendas.
    runs.run(datetime(2026, 11, 11, 8, tzinfo=EASTERN), reminders=True)
    assert runs.agenda_requests == ["8805", "8841"]
    # Once the meetings have started, their agendas aren't read.
    runs.agenda_requests.clear()
    runs.run(datetime(2026, 11, 12, 19, 30, tzinfo=EASTERN), reminders=True)
    assert runs.agenda_requests == []


def note_post(participation, title="Planning and Zoning Commission", zoom=True):
    meeting = {
        "title": title,
        "agenda": "https://norwalkct.portal.civicclerk.com/event/930/files",
        "when": "Nov 18, 2026 at 6:00 PM EST",
        "starts_at": datetime(2026, 11, 18, 18, tzinfo=EASTERN),
        "location": None,
        "zoom": "https://norwalkct-org.zoom.us/j/1" if zoom else None,
        "participation": participation,
    }
    [post] = meeting_posts(meeting, datetime(2026, 11, 18, 7, tzinfo=EASTERN))
    return post


def test_meeting_post_notes_a_public_hearing():
    post = note_post(Participation(hearings=["FYE 2028 OPERATING BUDGET"]), zoom=False)
    assert post.build().build_text() == (
        "Planning and Zoning Commission\nNov 18, 2026 at 6:00 PM EST\n"
        "🗣 Public hearing: FYE 2028 OPERATING BUDGET"
    )
    # The note doesn't change how the post is deduplicated.
    assert SeenPosts.key(post) == (
        "https://norwalkct.portal.civicclerk.com/event/930/files on 2026-11-18"
    )


def test_meeting_post_notes_public_comment_and_hearings():
    post = note_post(
        Participation(
            comment=True,
            hearings=[
                (
                    "Naming of the Police Headquarters Plaza in honor of Retired "
                    "Police Chief and Former Mayor Harry W. Rilling."
                ),
                "Proposed disposition of City property",
            ],
        )
    )
    # More than one hearing: a count, no topics.
    assert post.build().build_text() == (
        "Planning and Zoning Commission\nNov 18, 2026 at 6:00 PM EST\n"
        "🗣 Public comment on the agenda\n"
        "🗣 Public hearings (2)\n"
        "Join on Zoom"
    )


def test_long_hearing_topic_is_left_out_not_truncated():
    topic = "Proposed disposition of City property abutting 18/20 Cleveland Terrace"
    assert len(topic) > 60
    post = note_post(Participation(comment=True, hearings=[topic]))
    assert post.build().build_text() == (
        "Planning and Zoning Commission\nNov 18, 2026 at 6:00 PM EST\n"
        "🗣 Public comment on the agenda\n"
        "🗣 Public hearing\n"
        "Join on Zoom"
    )


def test_meeting_post_without_participation_has_no_note():
    for participation in (None, Participation()):
        post = note_post(participation, zoom=False)
        assert post.build().build_text() == (
            "Planning and Zoning Commission\nNov 18, 2026 at 6:00 PM EST"
        )


def test_note_is_dropped_to_fit():
    one = Participation(comment=True, hearings=["FYE 2028 OPERATING BUDGET"])
    for length in range(30, 200, 7):
        title = ("Special Meeting " * 20)[:length].strip()
        text = note_post(one, title).build().build_text()
        assert len(text) <= Post.MAX_POST_LENGTH
        assert text.startswith(f"{title}\nNov 18, 2026 at 6:00 PM EST")
        assert text.endswith("Join on Zoom")
    assert participation_note(one, 200) == (
        "🗣 Public comment on the agenda\n🗣 Public hearing: FYE 2028 OPERATING BUDGET"
    )
    # Without room for the topic, then the hearing line, then anything.
    assert participation_note(one, 60) == (
        "🗣 Public comment on the agenda\n🗣 Public hearing"
    )
    assert participation_note(one, 40) == "🗣 Public comment on the agenda"
    assert participation_note(Participation(comment=True), 20) == ""
    assert participation_note(Participation(hearings=["a", "b"]), 30) == (
        "🗣 Public hearings (2)"
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


SUMMARY = "AI summary of the agenda:\n• Item one\n• Item two"


def summary_replies(runs):
    return [(post, ref) for post, ref in runs.sent if post.reply_kind == "summary"]


def test_agendas_are_summarized_once_under_the_day_before_post():
    runs = MeetingRuns()
    # The 7 AM run posts tomorrow's meetings and summarizes their agendas.
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
        SUMMARY,
        SUMMARY,
    ]
    assert runs.summarizer.call_count == 2
    text, instructions, max_chars = runs.summarizer.call_args_list[0].args
    assert "Authorize the Purchasing Agent to issue a purchase order" in text
    assert "2 or 3 short bullet points" in instructions
    assert len("AI summary of the agenda:\n") + max_chars <= 300

    # Later runs that day post nothing new, so the LLM isn't asked again.
    for hour in range(8, 23):
        now = datetime(2026, 11, 11, hour, 5, tzinfo=EASTERN)
        assert runs.run(now, reminders=True, summaries=True) == []
    # On the day, the meetings are posted again, without a second summary.
    assert (
        runs.run(datetime(2026, 11, 12, 7, 5, tzinfo=EASTERN), summaries=True)
        == NOV_12_MEETINGS
    )
    assert runs.summarizer.call_count == 2

    roots = {
        card(post).title: ref
        for post, ref in runs.sent[: len(NOV_11_MEETINGS) + len(NOV_12_MEETINGS)]
    }
    replies = [post for post, _ in summary_replies(runs)]
    assert [r.reply.root.uri for r in replies] == [
        roots["Finance & Claims Committee"].uri,
        roots["Library Board of Directors"].uri,
    ]
    assert all(r.reply.parent.uri == r.reply.root.uri for r in replies)


def test_agenda_published_late_is_summarized_on_the_day():
    runs = MeetingRuns()
    # Not published yet (as the saved agenda 8835 isn't).
    runs.agendas = {"8805": saved_agenda("8835")}
    runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True)
    assert runs.summarizer.call_count == 1  # The Library Board's agenda only.

    runs.agendas = {}
    assert runs.run(datetime(2026, 11, 12, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_12_MEETINGS,
        SUMMARY,
    ]
    assert runs.summarizer.call_count == 2
    [*_, (finance_today, finance_ref), _, (summary, _)] = runs.sent
    assert card(finance_today).title == "Finance & Claims Committee"
    assert summary.reply.root.uri == finance_ref.uri


def test_failed_summary_is_retried_only_with_the_next_meeting_post():
    runs = MeetingRuns()
    runs.summarizer.return_value = None
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
    ]
    for hour in range(8, 23):
        runs.run(datetime(2026, 11, 11, hour, 5, tzinfo=EASTERN), summaries=True)
    assert runs.summarizer.call_count == 2

    runs.summarizer.return_value = "• Item one\n• Item two"
    assert runs.run(datetime(2026, 11, 12, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_12_MEETINGS,
        SUMMARY,
        SUMMARY,
    ]
    assert runs.summarizer.call_count == 4


def test_unreadable_agenda_skips_the_summary_not_the_meeting():
    runs = MeetingRuns()
    runs.agendas_up = False
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
    ]
    runs.summarizer.assert_not_called()


def test_summaries_reuse_the_agendas_read_for_the_meeting_posts():
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True)
    # Each upcoming meeting's agenda is read once, for both its post and summary.
    assert sorted(runs.agenda_requests) == ["8805", "8841"]
    assert runs.summarizer.call_count == 2


def test_no_summaries_when_no_meeting_was_posted():
    runs = MeetingRuns()
    runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN))
    assert runs.run(datetime(2026, 11, 11, 8, 5, tzinfo=EASTERN), summaries=True) == []
    runs.summarizer.assert_not_called()


def test_without_an_llm_key_agendas_are_only_logged(caplog):
    runs = MeetingRuns()
    with patch.object(summarize, "configured", return_value=False):
        seen = SeenPosts()
        feeds = NorwalkFeeds(timedelta(hours=1))
        now = datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN)
        with (
            patch.object(
                norwalk_civic_clerk,
                "CivicClerkClient",
                side_effect=lambda _tenant: fake_client(
                    lambda request: httpx.Response(
                        200,
                        json=saved_agenda(request.url.path.rsplit("/", 1)[1])
                        if "/Meetings/" in request.url.path
                        else runs.events,
                    )
                ),
            ),
            patch.object(summarize, "summarize") as llm,
            caplog.at_level("INFO"),
        ):
            sent = publish(
                MagicMock(),
                [
                    ("meetings", lambda: feeds.get_todays_norwalk_meetings(now)),
                    ("summaries", lambda: feeds.get_agenda_summaries(seen, now)),
                ],
                seen,
            )
    assert sent == (len(NOV_11_MEETINGS) + len(NOV_12_MEETINGS), 0)
    llm.assert_not_called()
    assert "LLM_API_KEY not set; would summarize" in caplog.text
    assert "Finance & Claims Committee" in caplog.text


def test_summary_bullets_are_normalized():
    assert bullets("- One\n\n* Two\n  • Three  ") == "• One\n• Two\n• Three"


def llm_answer(content="• Item one\n• Item two"):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


class LLM:
    """An OpenAI-compatible API answering with ``responses`` in turn (an
    ``httpx.Response``, or an exception to raise), repeating the last one."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


def test_real_llm_client_summarizes_under_the_meeting_posts():
    runs = MeetingRuns()
    runs.llm = LLM(httpx.Response(200, json=llm_answer()))
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
        SUMMARY,
        SUMMARY,
    ]
    assert runs.result == (8, 0)
    assert len(runs.llm.requests) == 2


def test_bad_llm_key_turns_summaries_off_without_failing_the_run():
    runs = MeetingRuns()
    runs.llm = LLM(httpx.Response(401, json={"error": {"message": "bad key"}}))
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
    ]
    # The meetings went out, nothing failed, and after the rejected request
    # the LLM wasn't asked again (for the Library Board's agenda).
    assert runs.result == (6, 0)
    assert len(runs.llm.requests) == 1


def test_unreachable_llm_turns_summaries_off_without_failing_the_run():
    runs = MeetingRuns()
    runs.llm = LLM(httpx.ConnectTimeout("timed out"))
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
    ]
    assert runs.result == (6, 0)
    assert len(runs.llm.requests) == 1


def test_llm_failure_after_a_success_skips_only_that_summary():
    runs = MeetingRuns()
    runs.llm = LLM(
        httpx.Response(200, json=llm_answer()), httpx.ReadTimeout("timed out")
    )
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
        SUMMARY,
    ]
    assert runs.result == (7, 0)
    assert len(runs.llm.requests) == 2


def test_unexpected_error_in_summaries_is_not_a_run_failure():
    runs = MeetingRuns()
    runs.summarizer.side_effect = RuntimeError("bug")
    assert runs.run(datetime(2026, 11, 11, 7, 5, tzinfo=EASTERN), summaries=True) == [
        *NOV_11_MEETINGS,
        *NOV_12_MEETINGS,
    ]
    assert runs.result == (6, 0)
