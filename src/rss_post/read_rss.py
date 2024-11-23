import feedparser


def read_rss_items(url) -> list[dict]:
    feed = feedparser.parse(url)
    return feed.entries
