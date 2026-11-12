from unittest.mock import patch

import feedparser
import httpx

from rss_post import media
from rss_post.post import Post
from tests.conftest import FIXTURES


def entry(item_xml: str, namespaces: str = ""):
    feed = f"""<?xml version="1.0"?><rss version="2.0" {namespaces}><channel>
        <title>t</title><item><link>https://example.com/story/</link>
        {item_xml}</item></channel></rss>"""
    return feedparser.parse(feed).entries[0]


MEDIA_NS = 'xmlns:media="http://search.yahoo.com/mrss/"'


def test_image_from_media_content():
    item = entry(
        '<media:content url="https://example.com/a.jpg" medium="image" '
        'type="image/jpeg" />',
        MEDIA_NS,
    )
    assert media.image_from_item(item) == "https://example.com/a.jpg"


def test_image_from_media_thumbnail():
    item = entry('<media:thumbnail url="https://example.com/t.jpg" />', MEDIA_NS)
    assert media.image_from_item(item) == "https://example.com/t.jpg"


def test_image_from_enclosure_ignores_non_images():
    audio = entry(
        '<enclosure url="https://example.com/a.mp3" type="audio/mpeg" length="1"/>'
    )
    image = entry(
        '<enclosure url="https://example.com/e.png" type="image/png" length="0"/>'
    )
    assert media.image_from_item(audio) is None
    assert media.image_from_item(image) == "https://example.com/e.png"


def test_image_from_description_img_resolves_relative_urls():
    item = entry(
        "<description><![CDATA[<p><img src='/img/x.jpg'/>Text</p>]]></description>"
    )
    assert media.image_from_item(item) == "https://example.com/img/x.jpg"


def test_image_from_ct_mirror_fixture():
    item = feedparser.parse(str(FIXTURES / "ctmirror.xml")).entries[0]
    assert media.image_from_item(item) == (
        "https://ctmirror.org/wp-content/uploads/2025/05/"
        "2025_0530_SR_Session_254-1024x683.jpg"
    )


def test_no_image():
    assert media.image_from_item(entry("<description>Text</description>")) is None


def response(url, status_code=200, **kwargs):
    return httpx.Response(
        status_code, request=httpx.Request("GET", url), **kwargs
    )


def test_og_image_from_page():
    page = """<html><head>
        <meta property="og:image" content="/share.png"></head></html>"""
    url = "https://example.com/news/1"
    with patch("rss_post.media._get", return_value=response(url, text=page)):
        assert media.og_image_from_page(url) == "https://example.com/share.png"


def test_og_image_falls_back_to_twitter_image():
    page = '<meta name="twitter:image" content="https://cdn.example.com/t.jpg">'
    url = "https://example.com/news/1"
    with patch("rss_post.media._get", return_value=response(url, text=page)):
        assert media.og_image_from_page(url) == "https://cdn.example.com/t.jpg"


def test_og_image_unreachable_page_is_none():
    assert media.og_image_from_page("https://example.com/news/1") is None


def test_find_thumbnail_prefers_feed_image():
    with patch("rss_post.media.og_image_from_page") as og:
        assert media.find_thumbnail_url("https://x", "https://img") == "https://img"
    og.assert_not_called()


def test_download_thumbnail_accepts_images():
    url = "https://example.com/a.jpg"
    ok = response(url, content=b"jpeg", headers={"content-type": "image/jpeg"})
    with patch("rss_post.media._get", return_value=ok):
        assert media.download_thumbnail(url) == b"jpeg"


def test_download_thumbnail_rejects_non_images_and_oversize():
    url = "https://example.com/a.jpg"
    html = response(url, content=b"<html>", headers={"content-type": "text/html"})
    huge = response(
        url,
        content=b"x" * (media.MAX_THUMB_BYTES + 1),
        headers={"content-type": "image/png"},
    )
    with patch("rss_post.media._get", return_value=html):
        assert media.download_thumbnail(url) is None
    with patch("rss_post.media._get", return_value=huge):
        assert media.download_thumbnail(url) is None


def test_download_thumbnail_http_error_is_none():
    url = "https://example.com/a.jpg"
    with patch("rss_post.media._get", return_value=response(url, status_code=404)):
        assert media.download_thumbnail(url) is None


def test_card_description_strips_feed_boilerplate():
    assert (
        Post.card_description(
            "<p>Council met tonight.</p><p>The post Council meets appeared first "
            "on Norwalk Current.</p>"
        )
        == "Council met tonight."
    )


def test_card_description_marks_read_more_with_ellipsis():
    assert (
        Post.card_description("members of the same party [&#8230;]")
        == "members of the same party…"
    )
    assert Post.card_description("members of the same party […]") == (
        "members of the same party…"
    )


def test_card_description_caps_length_on_word_boundary():
    text = Post.card_description("word " * 100)
    assert len(text) <= Post.MAX_CARD_DESCRIPTION_LENGTH
    assert text.endswith("word…")


def test_with_embed_card_keeps_thumb_url():
    post = Post().with_embed_card("https://x", "T", "D", thumb_url="https://i")
    assert post.thumb_url == "https://i"
