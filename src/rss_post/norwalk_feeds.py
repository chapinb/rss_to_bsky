import argparse
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from rss_post import (
    norwalk_boe,
    norwalk_city_calendar,
    norwalk_civic_clerk,
    norwalk_library,
    norwalk_oyster,
)
from rss_post.bsky import Bluesky, Stdout
from rss_post.dedup import (
    CANCELLED,
    REPLY_PREFIXES,
    SeenPosts,
    daily_key,
    post_key,
)
from rss_post.logging_config import get_logger, setup_logging
from rss_post.media import image_from_item
from rss_post.post import Post
from rss_post.read_rss import read_rss_items

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")
# Meetings and events go out from the first successful run at or after this hour (Eastern).
MEETINGS_HOUR = 7
# The city's circular emblem ("The City of Norwalk, Est. 1651, Inc. 1913", from
# the norwalkct.gov homepage SVG), centered on a 1200x628 card so Bluesky's
# ~1.91:1 thumbnail crop keeps the ring text. Used for meeting cards: the
# CivicClerk portal is rendered by JavaScript and has no og:image of its own.
CITY_SEAL = Path(__file__).parent / "assets" / "city_seal.png"
# A meeting's reminder goes out from the first run this long before it starts.
# A bit over an hour so hourly runs hit it even when one starts a few minutes
# late; a second run inside the window skips the reminder already sent.
REMINDER_WINDOW = timedelta(minutes=75)
ZOOM_LINK_TEXT = "\nJoin on Zoom"
# Meeting posts note where the public can speak (see participation_note). A
# lone hearing's topic is shown only if it is at most MAX_HEARING_TOPIC
# characters; topics are never truncated.
PUBLIC_COMMENT_NOTE = "🗣 Public comment on the agenda"
PUBLIC_HEARING_NOTE = "🗣 Public hearing"
MAX_HEARING_TOPIC = 60
# Minutes go out from the first run after they're published, if published this
# recently: enough to catch up after missed runs, while a first run (or one
# after a long outage) doesn't post weeks of backlog.
MINUTES_FRESHNESS = timedelta(days=3)
# At most this many events a day from each city calendar (#13 plans a weekly
# look-ahead for the rest).
CITY_EVENTS_PER_DAY = 2


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


def card_post(item) -> Post:
    """A post with the item's headline as text and a link card for the item."""
    return (
        Post()
        .with_headline(item.title)
        .with_embed_card(
            item.link,
            item.title,
            item.get("description", ""),
            thumb_url=image_from_item(item),
        )
    )


def participation_note(
    participation: norwalk_civic_clerk.Participation | None, room: int
) -> str:
    """Lines saying where the public can speak, in at most ``room`` characters.

    "🗣 Public comment on the agenda" and/or a hearing line: "🗣 Public
    hearing: <topic>" for one hearing with a short topic, else "🗣 Public
    hearing" or "🗣 Public hearings (N)". Topics are never truncated: the
    topic, then the hearing line, then everything is dropped to fit.
    """
    if participation is None:
        return ""
    comment = [PUBLIC_COMMENT_NOTE] if participation.comment else []
    hearings = participation.hearings
    candidates = []
    if len(hearings) == 1:
        if len(hearings[0]) <= MAX_HEARING_TOPIC:
            candidates.append([*comment, f"{PUBLIC_HEARING_NOTE}: {hearings[0]}"])
        candidates.append([*comment, PUBLIC_HEARING_NOTE])
    elif hearings:
        candidates.append([*comment, f"{PUBLIC_HEARING_NOTE}s ({len(hearings)})"])
    candidates.append(comment)
    for lines in candidates:
        note = "\n".join(lines)
        if len(note) <= room:
            return note
    return ""


def meeting_post(meeting: dict, day: date) -> Post:
    """The meeting's name and time, a card for its agenda, and its Zoom link.

    When its agenda has public comment or a public hearing, a line or two
    says so (see ``participation_note``). Keyed by the agenda link and
    ``day``, so it goes out at most once a day.
    """
    details = " · ".join(filter(None, [meeting["when"], meeting["location"]]))
    headline = f"{meeting['title']}\n{meeting['when']}"
    room = Post.MAX_POST_LENGTH - len(headline) - 1  # less the note's newline
    if meeting["zoom"]:
        room -= len(ZOOM_LINK_TEXT)
    if note := participation_note(meeting.get("participation"), room):
        headline = f"{headline}\n{note}"
    post = (
        Post()
        .with_headline(headline)
        .with_embed_card(
            meeting["agenda"], meeting["title"], details, thumb_url=CITY_SEAL
        )
        # After the card, which keys the post by its link but not the day.
        .keyed_by(meeting["agenda"], day)
    )
    if meeting["zoom"]:
        post.with_link(ZOOM_LINK_TEXT, meeting["zoom"])
    return post


def minutes_post(minutes: norwalk_civic_clerk.Minutes) -> Post:
    """A "Minutes posted: <meeting>, <date>" post with a card for the minutes."""
    meeting = minutes.meeting
    day = meeting["starts_at"].date()
    return (
        Post()
        .with_headline(
            f"{REPLY_PREFIXES['minutes']}: {meeting['title']}, "
            f"{day:%b} {day.day}, {day.year}"
        )
        .with_embed_card(
            minutes.url,
            minutes.name or f"{meeting['title']} minutes",
            f"{meeting['title']} · {meeting['when']}",
            thumb_url=CITY_SEAL,
        )
    )


class NorwalkFeeds:
    def __init__(self, posting_frequency: timedelta) -> None:
        self.posting_frequency = posting_frequency
        # Norwalk Oyster's events for a day, read once a run (see _oyster_event_names).
        self._oyster_names: tuple[date, list[str]] | None = None
        logger.info(
            f"Initialized NorwalkFeeds with posting frequency: {posting_frequency}"
        )

    def generate_with_embed_card(self, feed_name: str, feed_url: str) -> list[Post]:
        logger.info(f"Processing feed with embed card: {feed_name} from {feed_url}")
        items = read_rss_items(feed_url)
        posts = [
            card_post(item)
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

    def get_todays_norwalk_meetings(self, now: datetime | None = None) -> list[Post]:
        """Posts for today's and tomorrow's meetings that haven't started yet.

        Each is keyed by its agenda link and today's date, so later runs the
        same day skip what an earlier run posted, and tomorrow's meetings are
        posted again on the day.
        """
        now = now or datetime.now(EASTERN)
        logger.info("Fetching today's Norwalk meetings")
        meetings = norwalk_civic_clerk.get_todays_meetings(now, read_agendas=True)
        posts = []
        for meeting in meetings:
            if meeting["starts_at"] < now:
                logger.info(f"Skipping meeting that has started: {meeting['title']}")
                continue
            posts.append(meeting_post(meeting, now.astimezone(EASTERN).date()))
        logger.info(f"Generated {len(posts)} meeting posts")
        return posts

    def get_meeting_reminders(self, now: datetime | None = None) -> list[Post]:
        """Replies to today's meeting posts for meetings starting soon.

        Each reminder is threaded under the meeting's post from today (found
        by its agenda link and the day) and gives the start time and Zoom
        link. It goes out once, from the first run within ``REMINDER_WINDOW``
        of the start; if today's meeting post isn't found it is skipped.
        """
        now = now or datetime.now(EASTERN)
        today = now.astimezone(EASTERN).date()
        posts = []
        for meeting in norwalk_civic_clerk.get_todays_meetings(now):
            starts_at = meeting["starts_at"]
            if not now < starts_at <= now + REMINDER_WINDOW:
                continue
            if CANCELLED.search(meeting["title"]):
                logger.info(f"No reminder for cancelled meeting: {meeting['title']}")
                continue
            local = starts_at.astimezone(EASTERN)
            post = Post().with_text(
                f"{REPLY_PREFIXES['reminder']} {local.hour % 12 or 12}:{local:%M %p}"
            )
            if meeting["zoom"]:
                zoom = meeting["zoom"]
                post.with_text(" — Zoom: ").with_link(
                    zoom.removeprefix("https://").removeprefix("http://"), zoom
                )
            post.replying_to(daily_key(post_key(meeting["agenda"]), today), "reminder")
            posts.append(post)
        logger.info(f"Generated {len(posts)} meeting reminders")
        return posts

    def get_meeting_minutes(self, now: datetime | None = None) -> list[Post]:
        """Posts for meeting minutes published in the last ``MINUTES_FRESHNESS``.

        Each is a reply to the meeting's post from the day of the meeting, or
        failing that the day before. Minutes often come out after those posts
        have left the history (30 days), so then they're posted on their own.
        Either way the post has a card for the minutes, which keys it: a reply
        is skipped once sent, and minutes posted (standalone or as a reply)
        aren't posted again standalone.
        """
        now = now or datetime.now(EASTERN)
        posts = []
        for minutes in norwalk_civic_clerk.get_recent_minutes(now):
            if now - minutes.published_at > MINUTES_FRESHNESS:
                continue
            meeting = minutes.meeting
            root = post_key(meeting["agenda"], meeting["title"])
            day = meeting["starts_at"].date()
            post = minutes_post(minutes)
            for posted_on in (day - timedelta(days=1), day):
                post = (
                    minutes_post(minutes)
                    .replying_to(daily_key(root, posted_on), "minutes")
                    .otherwise(post)
                )
            posts.append(post)
        logger.info(f"Generated {len(posts)} meeting minutes posts")
        return posts

    def get_todays_boe_meetings(self, now: datetime | None = None) -> list[Post]:
        """Posts for today's and tomorrow's Board of Education meetings that
        haven't started yet.

        Like city meetings, each is keyed by its own link (its entry on the
        district's calendar) and today's date, so later runs the same day skip
        what an earlier run posted, and tomorrow's meetings are posted again
        on the day.
        """
        now = now or datetime.now(EASTERN)
        today = now.astimezone(EASTERN).date()
        posts = []
        meetings = norwalk_boe.get_meetings_on(today, today + timedelta(days=1))
        for meeting in meetings:
            if meeting.starts_at < now:
                logger.info(f"Skipping meeting that has started: {meeting.title}")
                continue
            title = f"Board of Education: {meeting.title}"
            details = " · ".join(filter(None, [meeting.when, meeting.location]))
            post = (
                Post()
                .with_headline(f"{title}\n{meeting.when}")
                .with_embed_card(meeting.url, title, details)
                # After the card, which keys the post by its link but not the day.
                .keyed_by(meeting.url, today)
                .with_link("\nAgendas on BoardDocs", norwalk_boe.AGENDAS_URL)
            )
            if meeting.zoom:
                post.with_link("\nJoin on Zoom", meeting.zoom)
            posts.append(post)
        logger.info(f"Generated {len(posts)} Board of Education meeting posts")
        return posts

    def get_norwalk_oyster_newsletter(self) -> list[Post]:
        logger.info("Processing Norwalk Oyster newsletter")
        now = datetime.now(UTC)
        posts = [
            Post()
            .with_headline(f"Norwalk Oyster: {issue.title}")
            .with_embed_card(issue.url, issue.title)
            for issue in norwalk_oyster.get_newsletter_issues()
            if now - issue.published <= self.posting_frequency
        ]
        logger.info(f"Generated {len(posts)} Norwalk Oyster newsletter posts")
        return posts

    def get_todays_norwalk_oyster_events(
        self, now: datetime | None = None
    ) -> list[Post]:
        """Posts for today's top-rated events that haven't started yet.

        Each event has its own page per date, so later runs the same day skip
        what an earlier run posted by its link.
        """
        now = now or datetime.now(EASTERN)
        posts = []
        for event in norwalk_oyster.get_events_on(now.astimezone(EASTERN).date()):
            if event.starts_at is not None and event.starts_at < now:
                logger.info(f"Skipping event that has started: {event.name}")
                continue
            details = ", ".join(filter(None, [event.when, event.venue]))
            headline = f"Today: {event.name}" + (f" ({details})" if details else "")
            posts.append(
                Post()
                .with_headline(headline)
                .with_embed_card(event.url, event.name, event.description)
            )
        logger.info(f"Generated {len(posts)} Norwalk Oyster event posts")
        return posts

    def get_todays_norwalk_library_events(
        self, now: datetime | None = None
    ) -> list[Post]:
        """Posts for today's notable library events that haven't started yet.

        Each event has its own page per date, so later runs the same day skip
        what an earlier run posted by its link.
        """
        now = now or datetime.now(EASTERN)
        posts = []
        for event in norwalk_library.get_events_on(now.astimezone(EASTERN).date()):
            if event.starts_at < now:
                logger.info(f"Skipping library event that has started: {event.title}")
                continue
            details = ", ".join(filter(None, [event.when, event.location]))
            posts.append(
                Post()
                .with_headline(f"Today at the library: {event.title} ({details})")
                .with_embed_card(
                    event.url, event.title, event.description, thumb_url=event.image
                )
            )
        logger.info(f"Generated {len(posts)} Norwalk Public Library event posts")
        return posts

    def get_todays_city_calendar_events(
        self, calendar: str, feed_url: str, now: datetime | None = None
    ) -> list[Post]:
        """Posts for today's events on a city calendar that haven't started yet.

        Events Norwalk Oyster also posts today are left to it. The first
        ``CITY_EVENTS_PER_DAY`` of the rest (by start time) are chosen before
        dropping started ones, so later runs pick the same events and the cap
        holds for the day; posted ones are skipped by their link.
        """
        now = now or datetime.now(EASTERN)
        today = now.astimezone(EASTERN).date()
        events = norwalk_city_calendar.get_events_on(feed_url, today)
        oyster = self._oyster_event_names(today)
        events = [
            event
            for event in events
            if not any(norwalk_city_calendar.same_event(event.name, n) for n in oyster)
        ][:CITY_EVENTS_PER_DAY]
        posts = []
        for event in events:
            if event.starts_at is not None and event.starts_at < now:
                logger.info(f"Skipping event that has started: {event.name}")
                continue
            details = ", ".join(filter(None, [event.when, event.place]))
            headline = f"Today: {event.name}" + (f" ({details})" if details else "")
            posts.append(
                Post()
                .with_headline(headline)
                .with_embed_card(
                    event.url,
                    event.name,
                    " · ".join(filter(None, [calendar, event.when, event.place])),
                    thumb_url=event.image,
                )
            )
        logger.info(f"Generated {len(posts)} {calendar} event posts")
        return posts

    def _oyster_event_names(self, day: date) -> list[str]:
        """Names of the Norwalk Oyster events posted on ``day``, read once a run.

        Best effort: if Norwalk Oyster can't be read, nothing is filtered.
        """
        if self._oyster_names is None or self._oyster_names[0] != day:
            try:
                names = [e.name for e in norwalk_oyster.get_events_on(day)]
            except (httpx.HTTPError, ValueError) as e:
                logger.warning(f"Could not read Norwalk Oyster events to skip ({e})")
                names = []
            self._oyster_names = (day, names)
        return self._oyster_names[1]

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
            norwalk_items.append(card_post(item))
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
        ("Norwalk Oyster newsletter", norwalk_feeds.get_norwalk_oyster_newsletter),
        ("meeting minutes", norwalk_feeds.get_meeting_minutes),
    ]
    current_hour = datetime.now(EASTERN).hour
    logger.info(f"Current hour (Eastern): {current_hour}")
    # Every run from 7 AM on reads the meetings; the ones already posted today
    # are skipped, so a failed or missed 7 AM run is caught up by the next one.
    if current_hour >= MEETINGS_HOUR:
        sources.append(("today's meetings", norwalk_feeds.get_todays_norwalk_meetings))
        # After the meetings, so a reminder can reply to a post made this run.
        sources.append(("meeting reminders", norwalk_feeds.get_meeting_reminders))
        sources.append(
            (
                "today's and tomorrow's Board of Education meetings",
                norwalk_feeds.get_todays_boe_meetings,
            )
        )
        sources.append(
            (
                "today's Norwalk Oyster events",
                norwalk_feeds.get_todays_norwalk_oyster_events,
            )
        )
        sources.append(
            (
                "today's Norwalk Public Library events",
                norwalk_feeds.get_todays_norwalk_library_events,
            )
        )
        for calendar, feed_url in [
            ("Norwalk Recreation & Parks", norwalk_city_calendar.REC_PARKS_URL),
            ("City of Norwalk", norwalk_city_calendar.MAIN_CITY_URL),
        ]:
            sources.append(
                (
                    f"today's {calendar} calendar events",
                    partial(
                        norwalk_feeds.get_todays_city_calendar_events,
                        calendar,
                        feed_url,
                    ),
                )
            )
    else:
        logger.info(f"Skipping meetings and events (before {MEETINGS_HOUR} AM)")

    try:
        seen = SeenPosts(client.published_posts())
    except Exception:
        # Posting without the history would repeat everything still in the
        # window, so skip this run; the next one picks up where this left off
        # as long as its window covers the gap.
        logger.exception("Failed to read the account's recent posts; not posting")
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


def publish(client, sources, seen: SeenPosts | None = None) -> tuple[int, int]:
    """Post everything new from each source, isolating failures.

    Posts already in ``seen`` (same link, same cancellation status) are
    skipped, and each post sent is added to it, so an item in two feeds goes
    out once. Replies are pointed at the posts they follow up (see
    ``Post.replying_to``); if those can't be found, the reply's fallback
    (``Post.otherwise``) is sent instead, or nothing. Posts made this run
    count, so a thread can be started and continued in one run. A source
    that raises, or a post that fails to send, is logged and skipped so the
    remaining sources and posts still go out.

    Returns:
        The number of posts sent and the number of failures.
    """
    seen = seen if seen is not None else SeenPosts()
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
            while not seen.resolve_reply(post) and getattr(post, "fallback", None):
                post = post.fallback
            if not seen.resolve_reply(post):
                logger.info(
                    f"Skipping {post.reply_kind} from {name}: "
                    "the post it replies to wasn't found"
                )
                continue
            if not seen.is_new(post):
                logger.info(
                    f"Skipping already-posted item from {name}: "
                    f"{post.link or seen.key(post)}"
                )
                continue
            try:
                ref = client.post(post)
            except Exception:
                logger.exception(f"Failed to send a post from {name}")
                failures += 1
                continue
            seen.add(post, ref)
            total_posts += 1
    return total_posts, failures


if __name__ == "__main__":
    main()
