from datetime import datetime, timedelta, timezone

from atproto import client_utils

from rss_post.post import Post
from rss_post.read_rss import read_rss_items


def posting_filter(item_pub_date: str, posting_frequency: timedelta) -> bool:
    pub_date = datetime.strptime(item_pub_date, "%a, %d %b %Y %H:%M:%S %z")
    return datetime.now(timezone.utc) - pub_date <= posting_frequency


class NorwalkFeeds:
    def __init__(self, posting_frequency: timedelta) -> None:
        self.posting_frequency = posting_frequency

    def generate_calendar_events(
        self, feed_name: str, feed_url: str
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
            if posting_filter(item.published, self.posting_frequency)
        ]

    def generate_without_title(
        self, feed_name: str, feed_url: str
    ) -> list[client_utils.TextBuilder]:
        items = read_rss_items(feed_url)
        return [
            Post()
            .from_feed(feed_name)
            .with_description(item.description)
            .with_link("Read more", item.link)
            .build()
            for item in items
            if posting_filter(item.published, self.posting_frequency)
        ]

    def get_committee_events(self) -> list[client_utils.TextBuilder]:
        return self.generate_calendar_events(
            "City of Norwalk CT Calendar",
            "https://www.norwalkct.gov/RSSFeed.aspx?ModID=58&CID=Calendar-of-Agency-Board-Commission-Comm-47",
        )

    def get_news_flashes(self) -> list[client_utils.TextBuilder]:
        return self.generate_without_title(
            "City of Norwalk CT News",
            "https://www.norwalkct.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
        )
