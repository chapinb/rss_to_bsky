"""Skipping items already posted, tested against the bot's real post history."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from atproto import models
from atproto_client.models.utils import get_or_create

from rss_post.bsky import Bluesky, Stdout
from rss_post.dedup import SeenLinks, normalize_link, published_links
from rss_post.post import Post
from tests.conftest import FIXTURES

# 100 posts by @norwalkctevents.bsky.social, Oct 27 - Nov 12 2026: 81 distinct
# links, 19 duplicate posts.
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
        ("http://norwalkct.gov/Calendar.aspx?EID=1", "https://www.norwalkct.gov/Calendar.aspx?EID=1"),
        ("https://ctmirror.org/2026/11/03/story/", "https://CTMirror.org/2026/11/03/story"),
        ("https://ctmirror.org/story/?utm_source=rss#more", "https://ctmirror.org/story"),
    ],
)
def test_normalize_link_ignores_trivial_differences(a, b):
    assert normalize_link(a) == normalize_link(b)


def test_normalize_link_keeps_identifying_query():
    assert normalize_link(
        "https://www.norwalkct.gov/Calendar.aspx?EID=17255"
    ) != normalize_link("https://www.norwalkct.gov/Calendar.aspx?EID=17256")


def test_published_links_reads_cards_from_saved_history():
    feed = saved_feed()
    feed.cursor = None
    links = published_links(
        client_returning(feed), "did:plc:x", since=datetime(2026, 10, 1, tzinfo=UTC)
    )

    assert len(links) == 81
    assert normalize_link("https://www.norwalkct.gov/Calendar.aspx?EID=17255") in links


def test_published_links_stops_at_since_without_reading_more_pages():
    client = client_returning(saved_feed())
    links = published_links(
        client, "did:plc:x", since=datetime(2026, 11, 10, tzinfo=UTC)
    )

    client.get_author_feed.assert_called_once()
    assert 0 < len(links) < 81


def test_published_links_follows_cursor():
    page = saved_feed()
    last = SimpleNamespace(feed=[], cursor=None)
    client = client_returning(page, last)

    published_links(client, "did:plc:x", since=datetime(2026, 1, 1, tzinfo=UTC))

    assert client.get_author_feed.call_args_list[1].kwargs["cursor"] == page.cursor


def test_published_links_includes_links_in_text_and_skips_reposts():
    def item(record, reason=None):
        return SimpleNamespace(post=SimpleNamespace(record=record), reason=reason)

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

    links = published_links(
        client_returning(page), "did:plc:x", since=datetime(2026, 11, 1, tzinfo=UTC)
    )

    assert links == {"https://a.example/1"}


def test_replaying_saved_history_posts_each_link_once():
    """Every duplicate in the bot's real history would have been skipped."""
    seen = SeenLinks()
    sent = 0
    for item in reversed(saved_feed().feed):
        post = Post().with_embed_card(item.post.record.embed.external.uri, "t")
        if seen.is_new(post):
            seen.add(post)
            sent += 1

    assert sent == 81


def test_posts_without_a_link_are_always_new():
    seen = SeenLinks(["https://a.example/1"])
    assert seen.is_new(Post().with_title("Meeting"))


@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_reads_its_own_history(mock_client_cls, _config):
    client = mock_client_cls.return_value
    client.login.return_value = SimpleNamespace(did="did:plc:me")
    client.get_author_feed.return_value = SimpleNamespace(feed=[], cursor=None)

    assert Bluesky().published_links() == set()
    assert client.get_author_feed.call_args.kwargs["actor"] == "did:plc:me"


@patch("rss_post.bsky.config", return_value="")
def test_dry_run_without_username_skips_nothing(_config):
    assert Stdout().published_links() == set()


@patch("rss_post.bsky.public_published_links", return_value={"https://a.example/1"})
@patch("rss_post.bsky.config", return_value="bot.bsky.social")
def test_dry_run_reads_public_history(_config, public):
    assert Stdout().published_links() == {"https://a.example/1"}
    public.assert_called_once_with("bot.bsky.social")
