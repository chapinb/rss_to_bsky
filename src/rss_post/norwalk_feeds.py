import argparse
from datetime import datetime, timedelta, timezone

import pytz
from atproto import client_utils

from rss_post.bsky import Bluesky, Stdout
from rss_post.post import Post
from rss_post.read_rss import read_rss_items
from rss_post import norwalk_civic_clerk


def posting_filter(item_pub_date: str, posting_frequency: timedelta) -> bool:
    pub_date = datetime.strptime(item_pub_date, "%a, %d %b %Y %H:%M:%S %z")
    return datetime.now(timezone.utc) - pub_date <= posting_frequency


class NorwalkFeeds:
    def __init__(self, posting_frequency: timedelta) -> None:
        self.posting_frequency = posting_frequency

    def generate_with_title(
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
        return self.generate_with_title(
            "City of Norwalk CT Calendar",
            "https://www.norwalkct.gov/RSSFeed.aspx?ModID=58&CID=Calendar-of-Agency-Board-Commission-Comm-47",
        )

    def get_news_flashes(self) -> list[client_utils.TextBuilder]:
        return self.generate_with_title(
            "City of Norwalk CT News",
            "https://www.norwalkct.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
        )

    def get_nancy_on_norwalk_stories(self) -> list[client_utils.TextBuilder]:
        return self.generate_with_title(
            "Nancy on Norwalk",
            "https://www.nancyonnorwalk.com/feed/",
        )

    def get_todays_norwalk_meetings(self) -> list[client_utils.TextBuilder]:
        meetings = norwalk_civic_clerk.get_todays_meetings()
        return [
            Post()
            .from_feed("City of Norwalk CT Meetings")
            .with_title(meeting["title"])
            .with_description(meeting["when"])
            .with_link("Agenda\n", meeting["agenda"])
            .with_link("Zoom Link", meeting["where"])
            .build()
            for meeting in meetings
        ]


def main():
    cli_args = argparse.ArgumentParser()
    cli_args.add_argument(
        "posting_frequency",
        type=int,
        default=1,
        help="Number of hours to consider for posting frequency",
    )
    cli_args.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print posts instead of sending them to the server",
    )
    args = cli_args.parse_args()

    norwalk_feeds = NorwalkFeeds(timedelta(hours=args.posting_frequency))

    if args.dry_run:
        client = Stdout()
    else:
        client = Bluesky()

    news = norwalk_feeds.get_news_flashes()
    for news_flash in news:
        client.post(news_flash)

    for committee_event in norwalk_feeds.get_committee_events():
        client.post(committee_event)

    for nancy_on_norwalk_story in norwalk_feeds.get_nancy_on_norwalk_stories():
        client.post(nancy_on_norwalk_story)

    if datetime.now(pytz.timezone("US/Eastern")).hour == 7:
        # Only run if it is in the 7:00 AM hour.
        for meeting in norwalk_feeds.get_todays_norwalk_meetings():
            client.post(meeting)


if __name__ == "__main__":
    main()
