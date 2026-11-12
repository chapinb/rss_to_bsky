from unittest.mock import patch

import feedparser
import pytest

from rss_post.read_rss import FeedUnavailableError, read_rss_items


def get_arbitrary_rss_data() -> str:
    return """
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>Sample RSS Feed</title>
                <link>http://www.example.com</link>
                <description>Sample RSS Feed</description>
                <item>
                    <title>Item 1</title>
                    <link>http://www.example.com/item1</link>
                    <description>Item 1 description</description>
                </item>
                <item>
                    <title>Item 2</title>
                    <link>http://www.example.com/item2</link>
                    <description>Item 2 description</description>
                </item>
            </channel>
        </rss>
    """


def test_read_rss_items():
    rss_data = get_arbitrary_rss_data()
    items = read_rss_items(rss_data)
    assert len(items) == 2
    assert items[0]["title"] == "Item 1"
    assert items[1]["title"] == "Item 2"


def test_unreachable_feed_raises():
    # What feedparser returns when the request itself fails.
    failed = feedparser.FeedParserDict(
        bozo=True, bozo_exception=OSError("connection refused"), entries=[], feed={}
    )
    with (
        patch("rss_post.read_rss.feedparser.parse", return_value=failed) as parse,
        patch("rss_post.read_rss.time.sleep"),
        pytest.raises(FeedUnavailableError, match="connection refused"),
    ):
        read_rss_items("https://example.com/feed")
    assert parse.call_count == 3


def test_transient_failure_is_retried():
    failed = feedparser.FeedParserDict(
        bozo=True, bozo_exception=OSError("reset"), entries=[], feed={}
    )
    ok = feedparser.parse(get_arbitrary_rss_data())
    with (
        patch("rss_post.read_rss.feedparser.parse", side_effect=[failed, ok]),
        patch("rss_post.read_rss.time.sleep") as sleep,
    ):
        assert len(read_rss_items("https://example.com/feed")) == 2
    sleep.assert_called_once_with(2.0)


def test_http_error_raises():
    failed = feedparser.FeedParserDict(status=503, bozo=False, entries=[], feed={})
    with (
        patch("rss_post.read_rss.feedparser.parse", return_value=failed),
        patch("rss_post.read_rss.time.sleep"),
        pytest.raises(FeedUnavailableError, match="HTTP 503"),
    ):
        read_rss_items("https://example.com/feed")


def test_empty_but_valid_feed_is_not_an_error():
    empty = """<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Empty</title></channel></rss>"""
    assert read_rss_items(empty) == []
