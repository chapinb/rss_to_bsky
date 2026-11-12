"""Skipping items already posted, tested against the bot's real post history."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from atproto import models
from atproto_client.models.utils import get_or_create

from rss_post.bsky import Bluesky, Stdout
from rss_post.dedup import (
    REPLY_PREFIXES,
    PostRef,
    SeenPosts,
    daily_key,
    keys_in_record,
    normalize_link,
    post_key,
    published_keys,
    published_posts,
    reply_key,
    reply_kind,
)
from rss_post.post import Post
from tests.conftest import FIXTURES

# 100 posts by @norwalkctevents.bsky.social, Oct 27 - Nov 12 2026: 81 distinct
# links, two of which (EID=17256, 17257) were later cancelled, and 17 repeats.
SAVED_FEED = FIXTURES / "bsky_author_feed.json"


def saved_feed():
    return get_or_create(
        json.loads(SAVED_FEED.read_text()), models.AppBskyFeedGetAuthorFeed.Response
    )


def client_returning(*pages):
    client = MagicMock()
    client.get_author_feed.side_effect = list(pages)
    return client


@pytest.mark.parametrize(
    "a, b",
    [
        (
            "http://norwalkct.gov/Calendar.aspx?EID=1",
            "https://www.norwalkct.gov/Calendar.aspx?EID=1",
        ),
        (
            "https://ctmirror.org/2026/11/03/story/",
            "https://CTMirror.org/2026/11/03/story",
        ),
        (
            "https://ctmirror.org/story/?utm_source=rss#more",
            "https://ctmirror.org/story",
        ),
    ],
)
def test_normalize_link_ignores_trivial_differences(a, b):
    assert normalize_link(a) == normalize_link(b)


def test_normalize_link_keeps_identifying_query():
    assert normalize_link(
        "https://www.norwalkct.gov/Calendar.aspx?EID=17255"
    ) != normalize_link("https://www.norwalkct.gov/Calendar.aspx?EID=17256")


def test_published_keys_reads_cards_from_saved_history():
    feed = saved_feed()
    feed.cursor = None
    links = published_keys(
        client_returning(feed), "did:plc:x", since=datetime(2026, 10, 1, tzinfo=UTC)
    )

    # Each card's link, and the link as posted that (Eastern) day, for meetings.
    assert len({link for link in links if " on " not in link}) == 83
    assert len(links) == 168
    assert normalize_link("https://www.norwalkct.gov/Calendar.aspx?EID=17255") in links


def test_published_keys_stops_at_since_without_reading_more_pages():
    client = client_returning(saved_feed())
    links = published_keys(
        client, "did:plc:x", since=datetime(2026, 11, 10, tzinfo=UTC)
    )

    client.get_author_feed.assert_called_once()
    assert 0 < len(links) < 83


def test_published_keys_follows_cursor():
    page = saved_feed()
    last = SimpleNamespace(feed=[], cursor=None)
    client = client_returning(page, last)

    published_keys(client, "did:plc:x", since=datetime(2026, 1, 1, tzinfo=UTC))

    assert client.get_author_feed.call_args_list[1].kwargs["cursor"] == page.cursor


def test_published_keys_includes_links_in_text_and_skips_reposts():
    def item(record, reason=None):
        return SimpleNamespace(
            post=SimpleNamespace(record=record, uri="at://p/1", cid="c1"),
            reason=reason,
        )

    meeting = models.AppBskyFeedPost.Record(
        text="Agenda",
        created_at="2026-11-12T12:05:00Z",
        facets=[
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=0, byte_end=6),
                features=[models.AppBskyRichtextFacet.Link(uri="https://a.example/1")],
            )
        ],
    )
    repost = Post().with_embed_card("https://b.example/2", "t").embed
    reposted = models.AppBskyFeedPost.Record(
        text="", created_at="2026-11-12T12:05:00Z", embed=repost
    )
    page = SimpleNamespace(
        feed=[item(meeting), item(reposted, reason=object())], cursor=None
    )

    links = published_keys(
        client_returning(page), "did:plc:x", since=datetime(2026, 11, 1, tzinfo=UTC)
    )

    # The link, and the link as posted on Nov 12 (Eastern), for meetings.
    assert links == {"https://a.example/1", "https://a.example/1 on 2026-11-12"}


def replay_saved_history():
    """Run the bot's real posts, oldest first, through dedup; return those sent."""
    seen = SeenPosts()
    sent = []
    for item in reversed(saved_feed().feed):
        card = item.post.record.embed.external
        post = Post().with_embed_card(card.uri, card.title)
        if seen.is_new(post):
            seen.add(post)
            sent.append((card.uri, card.title))
    return sent


def test_replaying_saved_history_drops_repeats():
    """81 distinct links plus 2 cancellations; the other 17 posts were repeats."""
    assert len(replay_saved_history()) == 83


def test_cancellation_is_posted_once_after_the_original():
    titles = [
        title for uri, title in replay_saved_history() if uri.endswith("EID=17256")
    ]
    assert titles == [
        "Recreation, Parks & Cultural Affairs Committee Special Meeting",
        "Recreation, Parks & Cultural Affairs Committee Special Meeting (CANCELLED)",
    ]


@pytest.mark.parametrize(
    "title", ["Board (CANCELLED)", "Board - Canceled", "cancelled: Board"]
)
def test_cancelled_titles_get_their_own_key(title):
    assert post_key("https://a.example/1", title) != post_key("https://a.example/1")


def test_titles_that_only_change_wording_share_a_key():
    assert post_key("https://a.example/1", "One Time Event") == post_key(
        "https://a.example/1", "Bike/Walk Commission Special Meeting"
    )


def test_posts_without_a_link_are_always_new():
    seen = SeenPosts(["https://a.example/1"])
    assert seen.is_new(Post().with_title("Meeting"))


@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_reads_its_own_history(mock_client_cls, _config):
    client = mock_client_cls.return_value
    client.login.return_value = SimpleNamespace(did="did:plc:me")
    client.get_author_feed.return_value = SimpleNamespace(feed=[], cursor=None)

    assert Bluesky().published_posts() == {}
    assert client.get_author_feed.call_args.kwargs["actor"] == "did:plc:me"


@patch("rss_post.bsky.config", return_value="")
def test_dry_run_without_username_skips_nothing(_config):
    assert Stdout().published_posts() == {}


@patch(
    "rss_post.bsky.public_published_posts",
    return_value={"https://a.example/1": PostRef("at://p/1", "c1")},
)
@patch("rss_post.bsky.config", return_value="bot.bsky.social")
def test_dry_run_reads_public_history(_config, public):
    assert Stdout().published_posts() == {
        "https://a.example/1": PostRef("at://p/1", "c1")
    }
    public.assert_called_once_with("bot.bsky.social")


def feed_item(record, uri, cid="c"):
    return SimpleNamespace(
        post=SimpleNamespace(record=record, uri=uri, cid=cid), reason=None
    )


def meeting_record(created_at="2026-11-12T12:05:00Z", link="https://a.example/1"):
    return models.AppBskyFeedPost.Record(
        text="Agenda",
        created_at=created_at,
        facets=[
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=0, byte_end=6),
                features=[models.AppBskyRichtextFacet.Link(uri=link)],
            )
        ],
    )


def reply_record(root_uri, text="Starting at 7:00 PM"):
    ref = models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid="c")
    return models.AppBskyFeedPost.Record(
        text=text,
        created_at="2026-11-12T22:05:00Z",
        reply=models.AppBskyFeedPost.ReplyRef(root=ref, parent=ref),
    )


def test_published_posts_map_keys_to_the_latest_post():
    page = SimpleNamespace(
        feed=[
            feed_item(meeting_record("2026-11-12T12:05:00Z"), "at://p/new"),
            feed_item(meeting_record("2026-11-11T12:05:00Z"), "at://p/old"),
        ],
        cursor=None,
    )

    posts = published_posts(
        client_returning(page), "did:plc:x", since=datetime(2026, 11, 1, tzinfo=UTC)
    )

    assert posts["https://a.example/1"] == PostRef("at://p/new", "c")
    assert posts["https://a.example/1 on 2026-11-11"] == PostRef("at://p/old", "c")


def test_published_posts_reads_replies_by_thread_and_kind():
    client = client_returning(SimpleNamespace(feed=[], cursor=None))
    published_posts(client, "did:plc:x")
    assert client.get_author_feed.call_args.kwargs["filter"] == "posts_with_replies"


def test_reply_is_keyed_by_root_and_kind_from_its_text():
    record = reply_record("at://p/root", "Starting at 7:00 PM — Zoom: zoom.us/j/1")
    assert keys_in_record(record) == {reply_key("at://p/root", "reminder")}


def test_reply_kinds_have_distinct_prefixes():
    prefixes = list(REPLY_PREFIXES.values())
    assert len(set(prefixes)) == len(prefixes)
    assert all(
        reply_kind(prefix + " x") == kind for kind, prefix in REPLY_PREFIXES.items()
    )


def test_hand_written_reply_has_no_keys():
    """Replies written by hand don't stop the bot posting what they link."""
    record = reply_record("at://p/root", "See you there! https://a.example/1")
    assert keys_in_record(record) == set()


def test_reply_resolves_root_from_history_and_is_deduped_by_kind():
    root_key = daily_key(post_key("https://a.example/1"), date(2026, 11, 12))
    seen = SeenPosts({root_key: PostRef("at://p/root", "c")})
    reply = Post().with_text("Starting at 7:00 PM").replying_to(root_key, "reminder")

    assert seen.resolve_reply(reply)
    assert reply.reply.root.uri == reply.reply.parent.uri == "at://p/root"
    assert seen.is_new(reply)

    seen.add(reply, PostRef("at://p/reply", "c"))
    again = Post().replying_to(root_key, "reminder")
    assert seen.resolve_reply(again)
    assert not seen.is_new(again)
    # Another kind of follow-up to the same post is new.
    other = Post().replying_to(root_key, "recording")
    assert seen.resolve_reply(other) and seen.is_new(other)


def test_reply_without_a_root_post_is_not_resolved():
    reply = Post().replying_to("https://a.example/1 on 2026-11-12", "reminder")
    assert not SeenPosts().resolve_reply(reply)
    assert reply.reply is None


def test_reply_to_a_post_sent_this_run_and_continuing_the_thread():
    seen = SeenPosts()
    root = Post().with_embed_card("https://a.example/week", "This week")
    seen.add(root, PostRef("at://p/root", "c1"))
    first = Post().replying_to(root, "lookahead-1")
    assert seen.resolve_reply(first)
    seen.add(first, PostRef("at://p/first", "c2"))

    second = Post().replying_to(root, "lookahead-2", parent=first)
    assert seen.resolve_reply(second)
    assert second.reply.root.uri == "at://p/root"
    assert second.reply.parent.uri == "at://p/first"
