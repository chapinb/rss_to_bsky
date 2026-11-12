from unittest.mock import MagicMock, patch

import pytest

from rss_post.bsky import Bluesky, Stdout
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
