from atproto import client_utils

from rss_post.post import Post
from rss_post.read_rss import read_rss_items


def generate_calendar_events(
    feed_name: str, feed_url: str
) -> list[client_utils.TextBuilder]:
    items = read_rss_items(feed_url)
    return [
        Post()
        .from_feed(feed_name)
        .with_title(item.title)
        .with_description(item.description)
        .with_link("Read more", item.link)
        .build()
        for item in items
    ]


def generate_without_title(
    feed_name: str, feed_url: str
) -> list[client_utils.TextBuilder]:
    items = read_rss_items(feed_url)
    return [
        Post()
        .from_feed(feed_name)
        .with_description(item.description)
        .with_link("Read more", item.link)
        .build()
        for item in items
    ]


def get_committee_events() -> list[client_utils.TextBuilder]:
    return generate_calendar_events(
        "City of Norwalk CT Calendar",
        "https://www.norwalkct.gov/RSSFeed.aspx?ModID=58&CID=Calendar-of-Agency-Board-Commission-Comm-47",
    )


def get_news_flashes() -> list[client_utils.TextBuilder]:
    return generate_without_title(
        "City of Norwalk CT News",
        "https://www.norwalkct.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
    )
