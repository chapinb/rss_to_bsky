import feedparser
from feedparser import FeedParserDict

from rss_post.logging_config import get_logger

logger = get_logger(__name__)


class FeedUnavailableError(Exception):
    """The feed could not be fetched or contained nothing parseable."""


def read_rss(url) -> FeedParserDict:
    """Fetch and parse a feed.

    feedparser never raises on network or HTTP errors; it returns an empty
    feed instead. Those cases are surfaced as ``FeedUnavailableError`` so a
    broken source is reported rather than looking like "no new items".
    """
    logger.info(f"Fetching RSS feed from: {url}")
    feed = feedparser.parse(url)

    status = feed.get("status")
    if status is not None and status >= 400:
        raise FeedUnavailableError(f"HTTP {status} from {url}")
    if feed.bozo and not feed.entries:
        raise FeedUnavailableError(f"Could not read {url}: {feed.bozo_exception}")
    if feed.bozo:
        logger.warning(f"RSS feed has parsing issues: {feed.bozo_exception}")

    logger.info(
        f"Parsed RSS feed: {feed.feed.get('title', 'Unknown')} "
        f"with {len(feed.entries)} entries"
    )
    return feed


def read_rss_items(url) -> list[FeedParserDict]:
    return read_rss(url).entries
