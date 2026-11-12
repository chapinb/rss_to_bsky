import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from rss_post import norwalk_civic_clerk
from rss_post.bsky import Bluesky, Stdout
from rss_post.dedup import SeenLinks
from rss_post.logging_config import get_logger, setup_logging
from rss_post.post import Post
from rss_post.read_rss import read_rss_items

logger = get_logger(__name__)


def published_at(item) -> datetime | None:
    """The item's publish time in UTC, as normalized by feedparser."""
    parsed = item.get("published_parsed") or item.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def posting_filter(item, posting_frequency: timedelta) -> bool:
    """Whether a feed item was published within the posting window."""
    pub_date = published_at(item)
    if pub_date is None:
        logger.warning(f"Skipping item without a usable date: {item.get('link')}")
        return False
    return datetime.now(UTC) - pub_date <= posting_frequency


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
            Post().with_embed_card(item.link, item.title, item.get("description", ""))
            for item in items
            if posting_filter(item, self.posting_frequency)
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

    def get_norwalk_current_stories(self) -> list[Post]:
        # Formerly Nancy on Norwalk; nancyonnorwalk.com/feed/ redirects here.
        return self.generate_with_embed_card(
            "Norwalk Current",
            "https://www.norwalkcurrent.com/feed/",
        )

    def get_todays_norwalk_meetings(self) -> list[Post]:
        logger.info("Fetching today's Norwalk meetings")
        meetings = norwalk_civic_clerk.get_todays_meetings()
        posts = []
        for meeting in meetings:
            post = (
                Post()
                .from_feed("City of Norwalk CT Meetings")
                .with_title(meeting["title"])
                .with_description(meeting["when"])
                .with_link("Agenda\n", meeting["agenda"])
            )
            if meeting["where"]:
                post.with_link("Zoom Link", meeting["where"])
            posts.append(post)
        logger.info(f"Generated {len(posts)} meeting posts")
        return posts

    def get_ct_mirror_stories(self) -> list[Post]:
        feed_url = "https://ctmirror.org/feed/"
        logger.info("Processing CT Mirror stories, filtering for Norwalk mentions")
        items = read_rss_items(feed_url)
        norwalk_items = []
        for item in items:
            if not posting_filter(item, self.posting_frequency):
                continue
            text = "\n".join(
                [item.get("title", ""), item.get("description", "")]
                + [content.value for content in item.get("content", [])]
            )
            if "norwalk" not in text.lower():
                continue
            logger.debug(f"Found Norwalk mention in CT Mirror story: {item.title}")
            norwalk_items.append(
                Post().with_embed_card(
                    item.link, item.title, item.get("description", "")
                )
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

    sources: list[tuple[str, Callable[[], list[Post]]]] = [
        ("news flashes", norwalk_feeds.get_news_flashes),
        ("committee events", norwalk_feeds.get_committee_events),
        ("Norwalk Current stories", norwalk_feeds.get_norwalk_current_stories),
        ("CT Mirror stories", norwalk_feeds.get_ct_mirror_stories),
    ]
    current_hour = datetime.now(ZoneInfo("America/New_York")).hour
    logger.info(f"Current hour (Eastern): {current_hour}")
    if current_hour == 7:
        sources.append(("today's meetings", norwalk_feeds.get_todays_norwalk_meetings))
    else:
        logger.info("Skipping meetings (not 7 AM hour)")

    try:
        seen = SeenLinks(client.published_links())
    except Exception:
        # Posting without the history would repeat everything still in the
        # window, so skip this run; the next one picks up where this left off
        # as long as its window covers the gap.
        logger.exception("Failed to read already-posted links; not posting")
        sys.exit(1)

    total_posts, failures = publish(client, sources, seen)
    if failures:
        logger.error(
            f"Completed with {failures} failure(s). Total posts processed: {total_posts}"
        )
        sys.exit(1)
    logger.info(
        f"Application completed successfully. Total posts processed: {total_posts}"
    )


def publish(client, sources, seen: SeenLinks | None = None) -> tuple[int, int]:
    """Post everything new from each source, isolating failures.

    Posts whose link is in ``seen`` are skipped, and each link posted is
    added to it, so an item in two feeds goes out once. A source that raises,
    or a post that fails to send, is logged and skipped so the remaining
    sources and posts still go out.

    Returns:
        The number of posts sent and the number of failures.
    """
    seen = seen if seen is not None else SeenLinks()
    total_posts = failures = 0
    for name, get_posts in sources:
        logger.info(f"Processing {name}...")
        try:
            posts = get_posts()
        except Exception:
            logger.exception(f"Failed to read {name}")
            failures += 1
            continue
        for post in posts:
            if not seen.is_new(post):
                logger.info(f"Skipping already-posted item from {name}: {post.link}")
                continue
            try:
                client.post(post)
            except Exception:
                logger.exception(f"Failed to send a post from {name}")
                failures += 1
                continue
            seen.add(post)
            total_posts += 1
    return total_posts, failures


if __name__ == "__main__":
    main()
