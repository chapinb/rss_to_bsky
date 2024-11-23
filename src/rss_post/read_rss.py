import feedparser

from feedparser import FeedParserDict


def read_rss(url) -> FeedParserDict:
    feed = feedparser.parse(url)
    return feed


def read_rss_items(url) -> list[dict]:
    feed = read_rss(url)
    return feed.entries
