from datetime import datetime, timedelta, timezone
from unittest.mock import patch
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


@patch("rss_post.norwalk_feeds.datetime")
def test_posting_filter(mock_datetime):
    mock_datetime.now.return_value = datetime(2024, 11, 28, 9, 7, 54, 0, timezone.utc)
    mock_datetime.strptime = datetime.strptime
    pub_date = "Fri, 22 Nov 2024 09:07:54 -0500"
    frequency = timedelta(days=7)

    assert posting_filter(pub_date, frequency) is True
