"""Tests for replying with meeting recordings, using saved CivicClerk data."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from atproto import models
from libmuni.civicclerk import Event

from rss_post import norwalk_civic_clerk
from rss_post.dedup import PostRef, SeenPosts, keys_in_record
from rss_post.norwalk_civic_clerk import get_recent_meetings, youtube_id
from rss_post.norwalk_feeds import NorwalkFeeds, publish
from tests.conftest import FIXTURES
from tests.test_norwalk_civic_clerk import fake_client

# Response of GET https://norwalkct.api.civicclerk.com/v1/Events for
# 2026-11-05..2026-11-12, captured 2026-11-12: five meetings with a youtu.be
# recording in externalMediaUrl, two with their Zoom link there.
EVENTS = json.loads((FIXTURES / "civicclerk_recent_events.json").read_text("utf-8"))
EASTERN = ZoneInfo("America/New_York")
NOV_12_9AM = datetime(2026, 11, 12, 9, tzinfo=EASTERN)


def serve(requests: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/Meetings/"):
            return httpx.Response(404)  # Agendas; none saved here.
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json=EVENTS)

    return fake_client(handler)


def test_recent_meetings_reads_the_last_days_once():
    requests = []
    get_recent_meetings(14, NOV_12_9AM, serve(requests))

    [request] = requests
    query = request.url.params["$filter"]
    assert "ge 2026-10-29T00:00:00Z" in query
    assert "le 2026-11-12T23:59:59Z" in query


def test_recent_meetings_have_started_and_carry_their_recordings():
    meetings = get_recent_meetings(14, NOV_12_9AM, serve())

    # Today's 7 PM meetings haven't started.
    assert len(meetings) == len(EVENTS["value"]) - 2
    recordings = {m["title"]: m["recording"] for m in meetings if m["recording"]}
    assert recordings == {
        "Shellfish Commission": "https://www.youtube.com/watch?v=CNufoFftcKg",
        "Public Safety and General Government Committee Special Meeting": (
            "https://www.youtube.com/watch?v=m00TTiSC5e0"
        ),
        "Blight Hearings": "https://www.youtube.com/watch?v=OwWVWul_QWU",
        "Norwalk Commission on Gender Equity": (
            "https://www.youtube.com/watch?v=Z2vbN3J7hYs"
        ),
        "City Council": "https://www.youtube.com/watch?v=4izgC2wkPKY",
    }
    council = next(m for m in meetings if m["title"] == "City Council")
    assert council["recording_thumb"] == (
        "https://i.ytimg.com/vi/4izgC2wkPKY/hqdefault.jpg"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"externalMediaUrl": "https://youtu.be/4izgC2wkPKY"}, "4izgC2wkPKY"),
        (
            {"externalMediaUrl": "https://www.youtube.com/watch?v=4izgC2wkPKY&t=3"},
            "4izgC2wkPKY",
        ),
        ({"externalMediaUrl": "https://youtube.com/live/4izgC2wkPKY"}, "4izgC2wkPKY"),
        # Before the recording is up, the field may hold the Zoom link.
        ({"externalMediaUrl": "https://norwalkct-org.zoom.us/j/862841"}, None),
        ({"externalMediaUrl": ""}, None),
        ({"youtubeVideoId": "abcdefghijk", "externalMediaUrl": ""}, "abcdefghijk"),
    ],
)
def test_youtube_id(raw, expected):
    entry = {**EVENTS["value"][0], **raw}
    assert youtube_id(Event.from_api(entry)) == expected


def meeting_key(event_id: int, day: str) -> str:
    return f"https://norwalkct.portal.civicclerk.com/event/{event_id}/files on {day}"


def meeting_history() -> dict[str, PostRef]:
    return {
        # City Council (Nov 10): posted the day before and on the day.
        meeting_key(1049, "2026-11-09"): PostRef("at://p/council-before", "c"),
        meeting_key(1049, "2026-11-10"): PostRef("at://p/council-day", "c"),
        # Gender Equity (Nov 9): only the day-before post is in the history.
        meeting_key(1036, "2026-11-08"): PostRef("at://p/equity-before", "c"),
        # Shellfish Commission (Nov 5): on the day.
        meeting_key(996, "2026-11-05"): PostRef("at://p/shellfish-day", "c"),
    }


def run(history: dict[str, PostRef], now: datetime) -> list:
    """One run of the recordings source; what it sends is added to ``history``
    the way the next run reads it."""
    sent = []

    def send(post):
        sent.append((post, PostRef(f"at://p/rec{len(history)}-{len(sent)}", "c")))
        return sent[-1][1]

    client = MagicMock()
    client.post.side_effect = send
    feeds = NorwalkFeeds(timedelta(hours=1))
    with patch.object(
        norwalk_civic_clerk, "CivicClerkClient", side_effect=lambda _: serve()
    ):
        publish(
            client,
            [("recordings", lambda: feeds.get_meeting_recordings(now))],
            SeenPosts(history),
        )
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
            history[key] = ref
    return [post for post, _ in sent]


def test_recordings_reply_to_the_meeting_post_once():
    history = meeting_history()

    sent = run(history, NOV_12_9AM)

    # The Public Safety and Blight Hearings recordings have no meeting post to
    # reply to, so they are skipped.
    assert [post.build().build_text() for post in sent] == [
        "Recording: Shellfish Commission",
        "Recording: Norwalk Commission on Gender Equity",
        "Recording: City Council",
    ]
    assert [post.reply.root.uri for post in sent] == [
        "at://p/shellfish-day",
        "at://p/equity-before",
        "at://p/council-day",
    ]
    council = sent[-1]
    assert council.reply_kind == "recording"
    assert council.reply.parent.uri == council.reply.root.uri
    assert council.embed.external.uri == "https://www.youtube.com/watch?v=4izgC2wkPKY"
    assert council.embed.external.title == "City Council (Nov 10, 2026)"
    assert council.thumb_url == "https://i.ytimg.com/vi/4izgC2wkPKY/hqdefault.jpg"

    # Later runs find the replies in the history.
    assert run(history, NOV_12_9AM + timedelta(hours=1)) == []


def test_recordings_without_a_meeting_post_are_skipped():
    assert run({}, NOV_12_9AM) == []


def test_recording_shared_by_two_meetings_replies_to_each():
    events = EVENTS["value"]
    shared = [
        {**e, "externalMediaUrl": "https://youtu.be/4izgC2wkPKY"}
        if e["id"] == 1036
        else e
        for e in events
    ]
    history = meeting_history()
    with patch.dict(EVENTS, {"value": shared}):
        sent = run(history, NOV_12_9AM)

    uri = "https://www.youtube.com/watch?v=4izgC2wkPKY"
    assert [p.reply.root.uri for p in sent if p.embed.external.uri == uri] == [
        "at://p/equity-before",
        "at://p/council-day",
    ]


def test_recordings_only_for_meetings_in_the_lookback():
    # 15 days on, only meetings from Nov 10 on are read; of those, the City
    # Council has a recording (the others hold a Zoom link or were cancelled).
    sent = run(meeting_history(), datetime(2026, 11, 24, 9, tzinfo=EASTERN))
    assert [post.embed.external.title for post in sent] == [
        "City Council (Nov 10, 2026)"
    ]
