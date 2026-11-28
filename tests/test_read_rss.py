from unittest.mock import patch

import httpx
import pytest

from rss_post import http
from rss_post.read_rss import FeedUnavailableError, parse_feed, read_rss_items

FEED_URL = "https://example.com/feed"


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


def served(*outcomes):
    """Stand-in for ``http.get``: each call returns (or raises) the next outcome."""
    request = httpx.Request("GET", FEED_URL)
    return patch.object(
        http,
        "get",
        side_effect=[
            outcome
            if isinstance(outcome, Exception)
            else httpx.Response(200, content=outcome.encode(), request=request)
            for outcome in outcomes
        ],
    )


def test_read_rss_items():
    with served(get_arbitrary_rss_data()):
        items = read_rss_items(FEED_URL)
    assert len(items) == 2
    assert items[0]["title"] == "Item 1"
    assert items[1]["title"] == "Item 2"


def test_feed_is_fetched_with_a_size_and_time_limit():
    with served(get_arbitrary_rss_data()) as get:
        read_rss_items(FEED_URL)
    assert get.call_args.kwargs["max_bytes"] > 0
    assert get.call_args.kwargs["timeout"] > 0


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        http.FetchRefused("took longer than 30.0s"),
        http.FetchRefused("over the 10000000-byte limit"),
    ],
)
def test_unreachable_feed_raises_after_retries(error):
    with (
        served(error, error, error),
        patch("rss_post.read_rss.time.sleep"),
        pytest.raises(FeedUnavailableError, match=str(error)),
    ):
        read_rss_items(FEED_URL)


def test_transient_failure_is_retried():
    with (
        served(httpx.ReadTimeout("reset"), get_arbitrary_rss_data()),
        patch("rss_post.read_rss.time.sleep") as sleep,
    ):
        assert len(read_rss_items(FEED_URL)) == 2
    sleep.assert_called_once_with(2.0)


def test_http_error_raises():
    request = httpx.Request("GET", FEED_URL)
    error = httpx.HTTPStatusError(
        "Server error '503 Service Unavailable'",
        request=request,
        response=httpx.Response(503, request=request),
    )
    with (
        served(error, error, error),
        patch("rss_post.read_rss.time.sleep"),
        pytest.raises(FeedUnavailableError, match="503"),
    ):
        read_rss_items(FEED_URL)


def test_unparseable_feed_raises():
    with (
        served("not a feed", "not a feed", "not a feed"),
        patch("rss_post.read_rss.time.sleep"),
        pytest.raises(FeedUnavailableError, match="Could not read"),
    ):
        read_rss_items(FEED_URL)


def test_empty_but_valid_feed_is_not_an_error():
    empty = """<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Empty</title></channel></rss>"""
    assert parse_feed(empty.encode(), FEED_URL).entries == []
