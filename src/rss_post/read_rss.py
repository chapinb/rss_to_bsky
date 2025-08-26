import feedparser

from feedparser import FeedParserDict
from rss_post.logging_config import get_logger

logger = get_logger(__name__)


def read_rss(url) -> FeedParserDict:
    logger.info(f"Fetching RSS feed from: {url}")
    try:
        feed = feedparser.parse(url)
        if feed.bozo:
            logger.warning(f"RSS feed has parsing issues: {feed.bozo_exception}")
        logger.info(
            f"Successfully parsed RSS feed: {feed.feed.get('title', 'Unknown')} with {len(feed.entries)} entries"
        )
        return feed
    except Exception as e:
        logger.error(f"Failed to fetch RSS feed from {url}: {e}")
        raise


def read_rss_items(url) -> list[FeedParserDict]:
    feed = read_rss(url)
    return feed.entries
