"""Tests for posting meeting minutes, using saved CivicClerk data."""

import json
import re
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
from atproto import models

from rss_post import norwalk_civic_clerk
from rss_post.dedup import PostRef, SeenPosts, keys_in_record
from rss_post.norwalk_civic_clerk import get_recent_minutes
from rss_post.norwalk_feeds import CITY_SEAL, NorwalkFeeds, publish
from rss_post.post import Post
from tests.conftest import FIXTURES
from tests.test_norwalk_civic_clerk import fake_client

# Response of GET https://norwalkct.api.civicclerk.com/v1/Events for
# 2026-10-27..2026-11-05 (its two pages joined), captured 2026-11-12.
EVENTS = json.loads((FIXTURES / "civicclerk_minutes_events.json").read_text("utf-8"))[
    "value"
]
EASTERN = ZoneInfo("America/New_York")
PORTAL = "https://norwalkct.portal.civicclerk.com"


def serve(requests: list | None = None):
    """A client serving the saved events within each request's date range."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/Meetings/"):
            # Meeting posts read agendas (for public comment); none saved here.
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        query = request.url.params["$filter"]
        start, end = re.findall(r"(\d{4}-\d\d-\d\d)T", query)
        return httpx.Response(
            200,
            json={"value": [e for e in EVENTS if start <= e["eventDate"][:10] <= end]},
        )

    return fake_client(handler)


def test_minutes_cover_meetings_in_the_lookback():
    requests = []
    get_recent_minutes(datetime(2026, 11, 12, 9, tzinfo=EASTERN), serve(requests))

    query = requests[0].url.params["$filter"]
    assert "ge 2026-09-13T00:00:00Z" in query
    assert "le 2026-11-12T23:59:59Z" in query


def test_recent_minutes_link_the_file_in_the_portal():
    found = get_recent_minutes(datetime(2026, 11, 6, 12, tzinfo=EASTERN), serve())

    council = next(m for m in found if m.meeting["title"] == "City Council")
    assert council.file_id == 16343
    assert council.name == "City Council Minutes 10.27.26"
    assert council.url == f"{PORTAL}/event/1060/files/agenda/16343"
    assert council.published_at == datetime(2026, 10, 30, 12, 29, 54, 47000, EASTERN)
    assert council.meeting["agenda"] == f"{PORTAL}/event/1060/files"


def test_minutes_published_before_the_meeting_are_the_previous_meetings():
    # Fair Rent's Nov 4 meeting carries its Oct 7 minutes, published Nov 3.
    found = get_recent_minutes(datetime(2026, 11, 6, 12, tzinfo=EASTERN), serve())
    titles = [m.meeting["title"] for m in found]

    assert "Fair Rent Commission" not in titles
    # Not yet published at the time of the run (Nov 12).
    assert not any(t.startswith("Public Safety") for t in titles)


class Runs:
    """Hourly runs of meetings and minutes; what each posts is read back the
    way the next run reads the account's history."""

    def __init__(self):
        self.history: dict[str, PostRef] = {}
        self.sent: list[tuple[Post, PostRef]] = []

    def run(self, now: datetime, meetings: bool = False) -> list[Post]:
        def send(post):
            ref = PostRef(f"at://p/{len(self.sent) + 1}", "c")
            self.sent.append((post, ref))
            return ref

        client = MagicMock()
        client.post.side_effect = send
        already_sent = len(self.sent)
        feeds = NorwalkFeeds(timedelta(hours=1))
        sources = [("minutes", lambda: feeds.get_meeting_minutes(now))]
        if meetings:
            sources.insert(
                0, ("meetings", lambda: feeds.get_todays_norwalk_meetings(now))
            )
        with patch.object(
            norwalk_civic_clerk, "CivicClerkClient", side_effect=lambda _: serve()
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
        return [post for post, _ in sent]

    def meeting_post(self, title: str, day: date) -> PostRef:
        return next(
            ref
            for post, ref in self.sent
            if post.reply is None
            and post.embed.external.title == title
            and post.day == day
        )


def text(post: Post) -> str:
    return post.build().build_text()


def test_minutes_reply_to_the_meeting_post_from_the_day_of_the_meeting():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    runs.run(datetime(2026, 10, 28, 7, tzinfo=EASTERN), meetings=True)

    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN))

    replies = {text(p): p for p in posts if p.reply is not None}
    council = replies["Minutes posted: City Council, Oct 27, 2026"]
    root = runs.meeting_post("City Council", date(2026, 10, 27))
    assert council.reply.root.uri == council.reply.parent.uri == root.uri
    assert council.reply_kind == "minutes"
    assert council.embed.external.uri == f"{PORTAL}/event/1060/files/agenda/16343"
    assert council.embed.external.title == "City Council Minutes 10.27.26"
    assert council.thumb_url == CITY_SEAL
    # Posted the day before and on the day: the on-the-day post is the root.
    harbor = replies["Minutes posted: Harbor Management Commission, Oct 28, 2026"]
    on_the_day = runs.meeting_post("Harbor Management Commission", date(2026, 10, 28))
    assert harbor.reply.root.uri == on_the_day.uri

    # Later runs skip what's been posted, and pick up newly published minutes.
    assert runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN)) == []
    [forestry] = runs.run(datetime(2026, 10, 30, 15, tzinfo=EASTERN))
    assert text(forestry) == "Minutes posted: Urban Forestry Commission, Oct 27, 2026"
    assert forestry.reply is not None


def test_minutes_reply_to_the_day_before_post_if_that_is_all_there_is():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)

    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN))

    [harbor] = [p for p in posts if "Harbor" in text(p)]
    the_day_before = runs.meeting_post(
        "Harbor Management Commission", date(2026, 10, 27)
    )
    assert harbor.reply.root.uri == the_day_before.uri


def test_minutes_without_a_meeting_post_are_posted_on_their_own_once():
    runs = Runs()
    [sustainability, shellfish] = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN))

    assert shellfish.reply is None
    assert text(shellfish) == "Minutes posted: Shellfish Commission, Nov 5, 2026"
    assert shellfish.embed.external.uri == f"{PORTAL}/event/996/files/agenda/16369"
    assert shellfish.embed.external.description == (
        "Shellfish Commission · Nov 5, 2026 at 6:30 PM EST"
    )
    assert text(sustainability).startswith(
        "Minutes posted: Ad Hoc Sustainability and Resilience Committee"
    )
    assert runs.run(datetime(2026, 11, 6, 13, tzinfo=EASTERN)) == []


def test_minutes_replied_to_are_not_posted_again_once_the_meeting_post_is_gone():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    assert runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN))

    # The meeting posts leave the 30-day history; the replies are newer.
    runs.history = {
        key: ref for key, ref in runs.history.items() if "/files on " not in key
    }
    assert runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN)) == []


def test_only_recently_published_minutes_are_posted():
    # A first run doesn't post the backlog: of the 15 sets of minutes
    # published by Nov 12, only those from the last three days.
    runs = Runs()
    [safety] = runs.run(datetime(2026, 11, 12, 9, tzinfo=EASTERN))
    assert text(safety).startswith(
        "Minutes posted: Public Safety and General Government Committee"
    )
    assert len(runs.run(datetime(2026, 11, 2, 12, tzinfo=EASTERN))) == 5


def test_reply_with_a_card_is_also_keyed_by_its_link():
    ref = models.ComAtprotoRepoStrongRef.Main(uri="at://p/root", cid="c")
    post = Post().with_headline("Minutes posted: Board, Oct 1, 2026")
    post.with_embed_card("https://a.example/minutes", "Minutes")
    record = models.AppBskyFeedPost.Record(
        text=text(post),
        embed=post.embed,
        created_at="2026-10-02T12:00:00Z",
        reply=models.AppBskyFeedPost.ReplyRef(root=ref, parent=ref),
    )

    assert keys_in_record(record) == {
        "at://p/root minutes",
        "https://a.example/minutes",
    }


def alternatives() -> tuple[Post, Post, Post]:
    """Minutes replying to the day's post, else the day before's, else alone."""
    standalone = Post().with_embed_card("https://x.example/minutes", "t")
    on_eve = Post().replying_to("https://x.example/m on 1", "minutes")
    on_the_day = Post().replying_to("https://x.example/m on 2", "minutes")
    on_the_day.otherwise(on_eve.otherwise(standalone))
    return on_the_day, on_eve, standalone


def test_reply_falls_back_through_its_alternatives():
    root = PostRef("at://p/root", "c")
    client = MagicMock()
    on_the_day, on_eve, _ = alternatives()
    seen = SeenPosts({"https://x.example/m on 1": root})

    assert publish(client, [("a", lambda: [on_the_day])], seen) == (1, 0)
    assert client.post.call_args.args[0] is on_eve
    assert on_eve.reply.root.uri == root.uri

    client = MagicMock()
    on_the_day, _, standalone = alternatives()
    assert publish(client, [("a", lambda: [on_the_day])], SeenPosts()) == (1, 0)
    assert client.post.call_args.args[0] is standalone
