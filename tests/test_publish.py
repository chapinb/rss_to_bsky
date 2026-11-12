from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
from libmuni.civicclerk import CivicClerkClient

from rss_post import norwalk_civic_clerk
from rss_post.dedup import PostRef, SeenPosts, reply_key
from rss_post.norwalk_feeds import NorwalkFeeds, publish
from rss_post.post import Post


def test_publishes_every_post_from_every_source():
    client = MagicMock()
    sources = [("a", lambda: ["a1", "a2"]), ("b", lambda: ["b1"])]

    assert publish(client, sources) == (3, 0)
    assert [c.args[0] for c in client.post.call_args_list] == ["a1", "a2", "b1"]


def test_failing_source_does_not_stop_the_others():
    client = MagicMock()

    def broken():
        raise RuntimeError("feed down")

    sources = [("broken", broken), ("ok", lambda: ["ok1"])]

    assert publish(client, sources) == (1, 1)
    client.post.assert_called_once_with("ok1")


def test_failing_post_does_not_stop_the_rest():
    client = MagicMock()
    client.post.side_effect = [RuntimeError("rate limited"), None, None]
    sources = [("a", lambda: ["a1", "a2"]), ("b", lambda: ["b1"])]

    assert publish(client, sources) == (2, 1)
    assert client.post.call_count == 3


def card_post(url):
    return Post().with_embed_card(url, "t")


def test_skips_links_already_posted():
    client = MagicMock()
    sources = [
        (
            "a",
            lambda: [
                card_post("https://x.example/old"),
                card_post("https://x.example/new"),
            ],
        )
    ]

    assert publish(client, sources, SeenPosts(["https://x.example/old"])) == (1, 0)
    assert client.post.call_args.args[0].link == "https://x.example/new"


def test_same_link_in_two_sources_is_posted_once():
    client = MagicMock()
    sources = [
        ("a", lambda: [card_post("https://x.example/story")]),
        ("b", lambda: [card_post("http://www.x.example/story/")]),
    ]

    assert publish(client, sources) == (1, 0)


def test_failed_post_is_not_marked_as_posted():
    client = MagicMock()
    client.post.side_effect = [RuntimeError("rate limited"), None]
    post = card_post("https://x.example/story")
    sources = [("a", lambda: [post]), ("b", lambda: [post])]

    assert publish(client, sources) == (1, 1)


def test_cancellation_of_a_posted_item_is_posted():
    client = MagicMock()
    url = "https://x.example/meeting"
    seen = SeenPosts([url])
    sources = [
        (
            "a",
            lambda: [
                Post().with_embed_card(url, "Board"),
                Post().with_embed_card(url, "Board (CANCELLED)"),
            ],
        ),
    ]

    assert publish(client, sources, seen) == (1, 0)
    assert client.post.call_args.args[0].embed.external.title == "Board (CANCELLED)"


def test_unreadable_meetings_count_as_a_failure():
    client = MagicMock()
    civic_clerk = CivicClerkClient(
        "norwalkct",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ),
        min_request_interval=0,
    )
    feeds = NorwalkFeeds(timedelta(hours=1))
    with (
        patch.object(norwalk_civic_clerk, "CivicClerkClient", return_value=civic_clerk),
        patch.object(norwalk_civic_clerk.time, "sleep"),
    ):
        result = publish(client, [("meetings", feeds.get_todays_norwalk_meetings)])

    assert result == (0, 1)
    client.post.assert_not_called()


def test_reply_goes_out_after_the_post_it_replies_to_in_the_same_run():
    client = MagicMock()
    client.post.side_effect = [PostRef("at://p/root", "c1"), PostRef("at://p/r", "c2")]
    root = card_post("https://x.example/meeting")
    reply = Post().with_text("Soon").replying_to(root, "reminder")

    assert publish(client, [("a", lambda: [root]), ("b", lambda: [reply])]) == (2, 0)
    assert client.post.call_args.args[0].reply.root.uri == "at://p/root"


def test_reply_without_its_root_is_skipped_not_failed():
    client = MagicMock()
    reply = Post().with_text("Soon").replying_to("https://x.example/m", "reminder")

    assert publish(client, [("a", lambda: [reply])]) == (0, 0)
    client.post.assert_not_called()


def test_reply_already_posted_is_skipped():
    client = MagicMock()
    root = PostRef("at://p/root", "c")
    seen = SeenPosts(
        {"https://x.example/m": root, reply_key(root.uri, "reminder"): root}
    )
    reply = Post().with_text("Soon").replying_to("https://x.example/m", "reminder")

    assert publish(client, [("a", lambda: [reply])], seen) == (0, 0)
