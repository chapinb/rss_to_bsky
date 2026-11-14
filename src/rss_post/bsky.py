import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from atproto import Client, client_utils, models
from atproto.exceptions import NetworkError, RequestException
from decouple import config

from rss_post import media
from rss_post.dedup import (
    PostRef,
    public_published_posts,
    published_posts,
)
from rss_post.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# Every post is in English. Declaring it lets Bluesky's language filters,
# feeds and translation handle the posts.
POST_LANGS = ["en"]

# Waits between attempts at the login and the history read, which the whole
# run depends on: a brief outage (a 503 for a few seconds) shouldn't cost the
# run, and the hour's news with it. About a minute in all, well inside the
# hourly schedule.
RETRY_DELAYS = (5.0, 15.0, 45.0)


def is_transient(error: Exception) -> bool:
    """Whether retrying might help: network failures, timeouts and 5xx.

    A 429 isn't retried: the login limits are per 5 minutes and per day, so a
    retry a few seconds later fails again and spends more of the budget.
    Bad credentials (401) and bad requests (400) aren't either.
    """
    if isinstance(error, NetworkError):  # Includes timeouts, 409, 413, 502.
        return True
    if isinstance(error, RequestException):
        response = error.response
        return response is not None and response.status_code >= 500
    return False


def with_retries(what: str, call: Callable[[], T]) -> T:
    """``call()``, retrying transient failures after each of ``RETRY_DELAYS``.

    The last error, or the first one that isn't transient, is raised.
    """
    attempts = len(RETRY_DELAYS) + 1
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            return call()
        except Exception as e:
            if not is_transient(e):
                raise
            logger.warning(
                f"Attempt {attempt}/{attempts} to {what} failed ({e}); "
                f"retrying in {delay:g}s"
            )
            time.sleep(delay)
    return call()


def _thumbnail_url(post_obj: Any) -> str | Path | None:
    embed = getattr(post_obj, "embed", None)
    if embed is None:
        return None
    return media.find_thumbnail_url(
        embed.external.uri, getattr(post_obj, "thumb_url", None)
    )


class Bluesky:
    def __init__(self):
        logger.info("Initializing Bluesky client")
        self.client = Client()
        username = config("BSKY_USERNAME")
        logger.info(f"Logging in to Bluesky as {username}")
        password = config("BSKY_PASSWORD")
        self.profile = with_retries(
            "log in to Bluesky", lambda: self.client.login(username, password)
        )
        logger.info("Successfully logged in to Bluesky")

    def attach_thumbnail(self, post_obj: Any) -> None:
        """Upload the card's image and set it as the thumb, if one is usable.

        Never raises: a card without a thumbnail beats a missing post.
        """
        try:
            image_url = _thumbnail_url(post_obj)
            if image_url is None:
                return
            image = media.download_thumbnail(image_url)
            if image is None:
                return
            blob = self.client.upload_blob(image).blob
            post_obj.embed.external.thumb = blob
            logger.debug(f"Attached thumbnail {image_url}")
        except Exception as e:  # noqa: BLE001 - thumbnails are optional
            logger.warning(f"Posting card without thumbnail: {e}")

    def published_posts(self) -> dict[str, PostRef]:
        return with_retries(
            "read the account's recent posts",
            lambda: published_posts(self.client, self.profile.did),
        )

    def post(self, post_obj: Any) -> PostRef:
        """Send the post (as a reply, if it is one); return where it was created."""
        try:
            text_builder: client_utils.TextBuilder = post_obj.build()
            embed: models.AppBskyEmbedExternal.Main | None = getattr(
                post_obj, "embed", None
            )
            if embed is not None and embed.external.thumb is None:
                self.attach_thumbnail(post_obj)

            post_text: str = text_builder.build_text()
            logger.debug(f"Posting to Bluesky: {post_text[:50]}...")

            response = self.client.send_post(
                text_builder,
                embed=embed,
                reply_to=getattr(post_obj, "reply", None),
                langs=POST_LANGS,
            )
            logger.info(f"Successfully posted to Bluesky: {response.uri}")
            return PostRef(response.uri, response.cid)
        except Exception as e:
            logger.error(f"Failed to post to Bluesky: {e}")
            raise


class Stdout:
    def __init__(self) -> None:
        logger.info("Initializing dry-run mode (console output)")
        self.printed = 0

    def published_posts(self) -> dict[str, PostRef]:
        """What the account has posted, read publicly so dry runs skip it too."""
        username = config("BSKY_USERNAME", default="")
        if not username:
            logger.warning("BSKY_USERNAME not set; dry run won't skip posted items")
            return {}
        return public_published_posts(username)

    def post(self, post_obj: Any) -> PostRef:
        """Print the post, replies indented; return a stand-in ref for threading."""
        text = self.render(post_obj)
        reply = getattr(post_obj, "reply", None)
        if reply is not None:
            kind = getattr(post_obj, "reply_kind", None) or "reply"
            print(f"  ↳ [{kind.upper()} REPLY to {reply.parent.uri}]")
            text = "\n".join(f"    {line}" for line in text.splitlines())
        print(text)
        self.printed += 1
        return PostRef(f"at://dry-run/app.bsky.feed.post/{self.printed}", "dry-run")

    @staticmethod
    def render(post_obj: Any) -> str:
        text_builder: client_utils.TextBuilder = post_obj.build()
        embed: models.AppBskyEmbedExternal.Main | None = getattr(
            post_obj, "embed", None
        )

        post_text: str = text_builder.build_text()
        logger.debug(f"Dry-run post: {post_text[:50]}...")
        lines = [post_text]
        if embed:
            lines.append(f"[EMBED CARD: {embed.external.title} - {embed.external.uri}]")
            if embed.external.description:
                lines.append(f"  {embed.external.description}")
            lines.append(f"  [THUMBNAIL: {_thumbnail_url(post_obj) or 'none'}]")
        return "\n".join(lines)
