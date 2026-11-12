from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import feedparser
from atproto import client_utils

from rss_post.norwalk_feeds import posting_filter
from rss_post.post import Post


def test_build_formatted_post():
    feed_title = "Sample RSS Feed"
    item_title = "Item 1"
    item_link = "http://www.example.com/item1"
    item_description = "Item 1 description"

    formatted_post = (
        Post()
        .from_feed(feed_title)
        .with_title(item_title)
        .with_description(item_description)
        .with_link("Read more", item_link)
        .build()
    )

    assert isinstance(formatted_post, client_utils.TextBuilder)


def test_remove_html_formatting():
    html_text = "<p>Some text</p>With <br> tags"
    assert Post.remove_html_formatting(html_text) == "Some text With tags"


def test_truncate_description():
    post = Post()
    post.MAX_POST_LENGTH = 15

    truncated = post.truncate_description("This is a long description")
    assert truncated.strip() == "This is a..."
    assert post.post_length == 13


def test_with_headline_strips_html_without_trailing_newline():
    post = Post().with_headline("<b>Council</b> meets &amp; votes")
    assert post.build().build_text() == "Council meets & votes"
    assert post.post_length == len("Council meets & votes")


def test_with_headline_caps_length():
    text = Post().with_headline("word " * 100).build().build_text()
    assert len(text) <= Post.MAX_POST_LENGTH
    assert text.endswith("word…")


def parse_item(item_xml: str):
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel>
        <title>t</title><item><link>https://example.com/a</link>{item_xml}</item>
        </channel></rss>"""
    return feedparser.parse(feed).entries[0]


def item_published(published: str):
    return parse_item(f"<pubDate>{published}</pubDate>")


@patch("rss_post.norwalk_feeds.datetime", wraps=datetime)
def test_posting_filter(mock_datetime):
    mock_datetime.now.return_value = datetime(2024, 11, 28, 9, 7, 54, 0, UTC)
    item = item_published("Fri, 22 Nov 2024 09:07:54 -0500")

    assert posting_filter(item, timedelta(days=7)) is True
    assert posting_filter(item, timedelta(days=5)) is False


def test_posting_filter_skips_items_without_a_date():
    item = parse_item("")
    assert posting_filter(item, timedelta(days=7)) is False


@patch("rss_post.norwalk_feeds.datetime", wraps=datetime)
def test_posting_filter_falls_back_to_updated_date(mock_datetime):
    mock_datetime.now.return_value = datetime(2024, 11, 28, 0, 0, 0, 0, UTC)
    feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <title>t</title><entry><title>e</title>
        <updated>2024-11-27T12:00:00Z</updated></entry></feed>"""
    item = feedparser.parse(feed).entries[0]
    assert "published_parsed" not in item
    assert posting_filter(item, timedelta(days=1)) is True
