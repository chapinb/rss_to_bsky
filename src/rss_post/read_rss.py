import time

import feedparser
import httpx
from feedparser import FeedParserDict

from rss_post import http
from rss_post.logging_config import get_logger

logger = get_logger(__name__)

# Bounds on fetching a feed (see ``http.get``): Norwalk's and CT Mirror's
# feeds are well under 1 MB.
FEED_MAX_BYTES = 10_000_000
FEED_TIMEOUT = 30.0


class FeedUnavailableError(Exception):
    """The feed could not be fetched or contained nothing parseable."""


def read_rss(url) -> FeedParserDict:
    """Fetch and parse a feed.

    The fetch is bounded in time and size (``http.get``); feedparser only
    parses the bytes, so it never opens a connection of its own (it has no
    timeout). Fetch failures and feeds with nothing parseable are raised as
    ``FeedUnavailableError``, so a broken source is reported rather than
    looking like "no new items".
    """
    logger.info(f"Fetching RSS feed from: {url}")
    try:
        response = http.get(url, max_bytes=FEED_MAX_BYTES, timeout=FEED_TIMEOUT)
    except httpx.HTTPError as e:
        raise FeedUnavailableError(f"Could not fetch {url}: {e}") from e
    return parse_feed(response.content, str(response.url), response.headers)


def parse_feed(
    content: bytes, url: str, headers: httpx.Headers | None = None
) -> FeedParserDict:
    """Parse a fetched feed; ``url`` resolves its relative links."""
    # feedparser calls a feed with headers but no content type malformed.
    content_type = (headers or {}).get("content-type", "application/xml")
    response_headers = {"content-location": url, "content-type": content_type}
    feed = feedparser.parse(content, response_headers=response_headers)

    if feed.bozo and not feed.entries:
        raise FeedUnavailableError(f"Could not read {url}: {feed.bozo_exception}")
    if feed.bozo:
        logger.warning(f"RSS feed has parsing issues: {feed.bozo_exception}")

    logger.info(
        f"Parsed RSS feed: {feed.feed.get('title', 'Unknown')} "
        f"with {len(feed.entries)} entries"
    )
    return feed


def read_rss_with_retries(
    url, attempts: int = 3, backoff_seconds: float = 2.0
) -> FeedParserDict:
    """``read_rss`` with retries, since a missed fetch means missed posts."""
    for attempt in range(1, attempts + 1):
        try:
            return read_rss(url)
        except FeedUnavailableError as e:
            if attempt == attempts:
                raise
            delay = backoff_seconds * attempt
            logger.warning(
                f"Attempt {attempt}/{attempts} failed ({e}); retrying in {delay}s"
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def read_rss_items(url) -> list[FeedParserDict]:
    return read_rss_with_retries(url).entries
