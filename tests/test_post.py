from atproto import client_utils

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
    html_text = "<p>Some text</p>"
    assert Post.remove_html_formatting(html_text) == "Some text"


def test_truncate_description():
    post = Post()
    post.MAX_POST_LENGTH = 15

    truncated = post.truncate_description("This is a long description")
    assert truncated == "This is a..."
    assert post.post_length == 12
