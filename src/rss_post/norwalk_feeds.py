import argparse
from datetime import datetime, timedelta, timezone

import pytz
from atproto import client_utils

from rss_post.bsky import Bluesky, Stdout
from rss_post.post import Post
from rss_post.read_rss import read_rss_items
from rss_post import norwalk_civic_clerk
from rss_post.logging_config import setup_logging, get_logger


logger = get_logger(__name__)


def posting_filter(item_pub_date: str, posting_frequency: timedelta) -> bool:
    pub_date = datetime.strptime(item_pub_date, "%a, %d %b %Y %H:%M:%S %z")
    return datetime.now(timezone.utc) - pub_date <= posting_frequency


class NorwalkFeeds:
    def __init__(self, posting_frequency: timedelta) -> None:
        self.posting_frequency = posting_frequency
        logger.info(
            f"Initialized NorwalkFeeds with posting frequency: {posting_frequency}"
        )

    def generate_with_embed_card(self, feed_name: str, feed_url: str) -> list[Post]:
        logger.info(f"Processing feed with embed card: {feed_name} from {feed_url}")
        items = read_rss_items(feed_url)
        posts = [
            Post()
            .with_embed_card(item.link, item.title, item.description)
            for item in items
            if posting_filter(item.published, self.posting_frequency)
        ]
        logger.info(f"Generated {len(posts)} posts from {feed_name}")
        return posts

    def get_committee_events(self) -> list[Post]:
        return self.generate_with_embed_card(
            "City of Norwalk CT Calendar",
            "https://www.norwalkct.gov/RSSFeed.aspx?ModID=58&CID=Calendar-of-Agency-Board-Commission-Comm-47",
        )

    def get_news_flashes(self) -> list[Post]:
        return self.generate_with_embed_card(
            "City of Norwalk CT News",
            "https://www.norwalkct.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
        )

    def get_nancy_on_norwalk_stories(self) -> list[Post]:
        return self.generate_with_embed_card(
            "Nancy on Norwalk",
            "https://www.nancyonnorwalk.com/feed/",
        )

    def get_todays_norwalk_meetings(self) -> list[Post]:
        logger.info("Fetching today's Norwalk meetings")
        meetings = norwalk_civic_clerk.get_todays_meetings()
        posts = [
            Post()
            .from_feed("City of Norwalk CT Meetings")
            .with_title(meeting["title"])
            .with_description(meeting["when"])
            .with_link("Agenda\n", meeting["agenda"])
            .with_link("Zoom Link", meeting["where"])
            for meeting in meetings
        ]
        logger.info(f"Generated {len(posts)} meeting posts")
        return posts

    def get_ct_mirror_stories(self) -> list[Post]:
        feed_url = "https://ctmirror.org/feed/"
        logger.info("Processing CT Mirror stories, filtering for Norwalk mentions")
        items = read_rss_items(feed_url)
        norwalk_items = []
        for item in items:
            if not posting_filter(item.published, self.posting_frequency):
                continue
            contents = "\n".join(content.value for content in item.content)
            if not (
                "norwalk" in item.title.lower()
                or "norwalk" in item.description.lower()
                or "norwalk" in contents.lower()
            ):
                continue
            logger.debug(f"Found Norwalk mention in CT Mirror story: {item.title}")
            norwalk_items.append(
                Post().with_embed_card(item.link, item.title, item.description)
            )
        logger.info(
            f"Generated {len(norwalk_items)} CT Mirror posts with Norwalk mentions"
        )
        return norwalk_items


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
    cli_args.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level",
    )
    args = cli_args.parse_args()

    setup_logging(level=args.log_level)
    logger.info("Starting RSS to Bluesky application")
    logger.info(f"Posting frequency: {args.posting_frequency} hours")
    logger.info(f"Dry run mode: {args.dry_run}")

    norwalk_feeds = NorwalkFeeds(timedelta(hours=args.posting_frequency))

    if args.dry_run:
        client = Stdout()
        logger.info("Using dry-run mode (console output)")
    else:
        client = Bluesky()
        logger.info("Using Bluesky client for posting")

    total_posts = 0

    logger.info("Processing news flashes...")
    news = norwalk_feeds.get_news_flashes()
    for news_flash in news:
        client.post(news_flash)
        total_posts += 1

    logger.info("Processing committee events...")
    for committee_event in norwalk_feeds.get_committee_events():
        client.post(committee_event)
        total_posts += 1

    logger.info("Processing Nancy on Norwalk stories...")
    for nancy_on_norwalk_story in norwalk_feeds.get_nancy_on_norwalk_stories():
        client.post(nancy_on_norwalk_story)
        total_posts += 1

    logger.info("Processing CT Mirror stories...")
    for ct_mirror_story in norwalk_feeds.get_ct_mirror_stories():
        client.post(ct_mirror_story)
        total_posts += 1

    current_hour = datetime.now(pytz.timezone("US/Eastern")).hour
    logger.info(f"Current hour (Eastern): {current_hour}")
    if current_hour == 7:
        logger.info("Processing today's meetings (7 AM hour)...")
        for meeting in norwalk_feeds.get_todays_norwalk_meetings():
            client.post(meeting)
            total_posts += 1
    else:
        logger.info("Skipping meetings (not 7 AM hour)")

    logger.info(
        f"Application completed successfully. Total posts processed: {total_posts}"
    )


if __name__ == "__main__":
    main()
