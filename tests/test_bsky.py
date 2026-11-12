from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from atproto import models

from rss_post.bsky import Bluesky, Stdout
from rss_post.dedup import PostRef, keys_in_record
from rss_post.post import Post


def test_stdout_prints_text_and_embed_card(capsys):
    post = (
        Post()
        .from_feed("Feed")
        .with_embed_card("https://example.com/a", "A title", "A description")
    )
    Stdout().post(post)

    out = capsys.readouterr().out
    assert "Feed\n" in out
    assert "[EMBED CARD: A title - https://example.com/a]" in out
    assert "  A description" in out
    assert "[THUMBNAIL: none]" in out


def test_stdout_shows_feed_thumbnail(capsys):
    post = Post().with_embed_card(
        "https://example.com/a", "T", thumb_url="https://example.com/i.jpg"
    )
    Stdout().post(post)
    assert "[THUMBNAIL: https://example.com/i.jpg]" in capsys.readouterr().out


def test_stdout_without_embed_prints_only_text(capsys):
    Stdout().post(Post().with_title("Just text"))

    out = capsys.readouterr().out
    assert "Just text" in out
    assert "EMBED CARD" not in out


@patch("rss_post.bsky.config", side_effect=lambda key: f"<{key}>")
@patch("rss_post.bsky.Client")
def test_bluesky_logs_in_and_sends_post_with_embed(mock_client_cls, _config):
    client = mock_client_cls.return_value
    post = Post().with_embed_card("https://example.com/a", "A title")

    Bluesky().post(post)

    client.login.assert_called_once_with("<BSKY_USERNAME>", "<BSKY_PASSWORD>")
    client.send_post.assert_called_once()
    args, kwargs = client.send_post.call_args
    assert args[0] is post.text_builder
    assert kwargs["embed"].external.uri == "https://example.com/a"


@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_reraises_send_failures(mock_client_cls, _config):
    mock_client_cls.return_value.send_post = MagicMock(side_effect=RuntimeError)
    bsky = Bluesky()

    with pytest.raises(RuntimeError):
        bsky.post(Post().with_title("t"))


@patch("rss_post.bsky.media.download_thumbnail", return_value=b"jpeg")
@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_uploads_thumbnail_for_card(mock_client_cls, _config, download):
    client = mock_client_cls.return_value
    post = Post().with_embed_card(
        "https://example.com/a", "T", thumb_url="https://example.com/i.jpg"
    )

    Bluesky().post(post)

    download.assert_called_once_with("https://example.com/i.jpg")
    client.upload_blob.assert_called_once_with(b"jpeg")
    embed = client.send_post.call_args.kwargs["embed"]
    assert embed.external.thumb is client.upload_blob.return_value.blob


@patch("rss_post.bsky.media.og_image_from_page", return_value="https://og/i.png")
@patch("rss_post.bsky.media.download_thumbnail", return_value=b"png")
@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_falls_back_to_page_og_image(mock_client_cls, _config, download, og):
    post = Post().with_embed_card("https://example.com/a", "T")

    Bluesky().post(post)

    og.assert_called_once_with("https://example.com/a")
    download.assert_called_once_with("https://og/i.png")


@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_posts_card_without_thumb_when_upload_fails(mock_client_cls, _config):
    client = mock_client_cls.return_value
    client.upload_blob.side_effect = RuntimeError("blob too large")
    post = Post().with_embed_card(
        "https://example.com/a", "T", thumb_url="https://example.com/i.jpg"
    )

    with patch("rss_post.bsky.media.download_thumbnail", return_value=b"jpeg"):
        Bluesky().post(post)

    client.send_post.assert_called_once()
    assert client.send_post.call_args.kwargs["embed"].external.thumb is None


ROOT = PostRef("at://did:plc:me/app.bsky.feed.post/root", "cid-root")


@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_returns_ref_of_created_post(mock_client_cls, _config):
    client = mock_client_cls.return_value
    client.send_post.return_value = SimpleNamespace(uri="at://p/1", cid="c1")

    assert Bluesky().post(Post().with_title("t")) == PostRef("at://p/1", "c1")


@patch("rss_post.bsky.config", return_value="x")
@patch("rss_post.bsky.Client")
def test_bluesky_sends_reply_with_root_and_parent(mock_client_cls, _config):
    client = mock_client_cls.return_value
    client.send_post.return_value = SimpleNamespace(uri="at://p/2", cid="c2")
    post = (
        Post()
        .with_text("Starting at 7:00 PM — Zoom: ")
        .with_link("zoom.us/j/1", "https://zoom.us/j/1")
        .replying_to("key", "reminder")
        .reply_to(ROOT)
    )

    assert Bluesky().post(post) == PostRef("at://p/2", "c2")

    args, kwargs = client.send_post.call_args
    assert args[0] is post.text_builder
    reply = kwargs["reply_to"]
    assert reply.root.uri == reply.parent.uri == ROOT.uri
    assert reply.root.cid == ROOT.cid
    # What later runs read back: the record send_post makes from these.
    record = models.AppBskyFeedPost.Record(
        text=args[0].build_text(),
        facets=args[0].build_facets(),
        reply=reply,
        created_at="2026-11-12T23:05:00Z",
    )
    assert record.facets[0].features[0].uri == "https://zoom.us/j/1"
    assert keys_in_record(record) == {f"{ROOT.uri} reminder"}


def test_stdout_prints_reply_indented_under_its_parent(capsys):
    post = (
        Post()
        .with_text("Starting at 7:00 PM")
        .replying_to("key", "reminder")
        .reply_to(ROOT)
    )
    Stdout().post(post)

    assert capsys.readouterr().out == (
        f"  ↳ [REMINDER REPLY to {ROOT.uri}]\n    Starting at 7:00 PM\n"
    )


def test_stdout_returns_distinct_refs_so_dry_runs_can_thread():
    stdout = Stdout()
    first = stdout.post(Post().with_title("a"))
    second = stdout.post(Post().with_text("b").reply_to(first))
    assert first != second
