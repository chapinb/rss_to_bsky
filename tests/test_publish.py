from unittest.mock import MagicMock

from rss_post.norwalk_feeds import publish


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
