"""Skip items the bot has already posted, using its own Bluesky posts as the record.

Feeds re-surface items the bot has already posted: the city feeds label EDT
times as ``-0500`` (items look an hour in the future and pass two hourly
windows), edited items get a new ``pubDate``, and live stories are updated in
place. Rather than chase each timestamp quirk, every run reads the links the
account has posted recently and skips any item whose link is among them.

The one change worth a second post is a cancellation: an item is keyed by its
link and whether its title says it is cancelled, so "Board Meeting" and later
"Board Meeting (CANCELLED)" both go out, but each only once.

Meetings are posted the day before and again on the day, so a meeting post is
keyed by its agenda link and the (Eastern) day it was posted: it goes out at
most once a day, however many runs see it.
"""

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from atproto import Client

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

# Items have come back as much as five days after first being posted.
HISTORY = timedelta(days=30)
PAGE_SIZE = 100
MAX_PAGES = 20

PUBLIC_APPVIEW = "https://public.api.bsky.app/xrpc"

EASTERN = ZoneInfo("America/New_York")

CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)


def normalize_link(url: str) -> str:
    """A canonical form of ``url`` so trivially different links compare equal.

    Ignores the scheme, host case, ``www.``, fragment, trailing slash and
    ``utm_*`` tracking parameters. The rest of the query is kept because it
    identifies the item on the city's site (``Calendar.aspx?EID=17255``).
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        ]
    )
    return urlunsplit(("https", host, path, query, ""))


def post_key(link: str, title: str = "") -> str:
    """What makes a post a repeat: its link, and whether it announces a cancellation."""
    key = normalize_link(link)
    return f"{key} cancelled" if CANCELLED.search(title) else key


def daily_key(key: str, day: date) -> str:
    """``key`` for a post that may go out once per day."""
    return f"{key} on {day.isoformat()}"


def keys_in_record(record, posted: datetime | None = None) -> set[str]:
    """Keys of what a post record points at: its link card and any links in its text.

    Links in the text also get a daily key for the Eastern day the record was
    posted (``posted``), using the text to tell whether it is a cancellation.
    """
    keys = set()
    external = getattr(getattr(record, "embed", None), "external", None)
    if external is not None:
        keys.add(post_key(external.uri, external.title or ""))
    day = posted.astimezone(EASTERN).date() if posted is not None else None
    for facet in getattr(record, "facets", None) or []:
        for feature in facet.features:
            uri = getattr(feature, "uri", None)
            if uri:
                keys.add(post_key(uri))
                if day is not None:
                    keys.add(daily_key(post_key(uri, record.text or ""), day))
    return keys


def published_keys(
    client: Client, actor: str, since: datetime | None = None
) -> set[str]:
    """Keys of what ``actor`` has posted since ``since`` (default: 30 days ago).

    Raises whatever the client raises: without the history the run can't tell
    what is new, so callers should not post.
    """
    since = since or datetime.now(UTC) - HISTORY
    keys: set[str] = set()
    cursor = None
    posts = 0
    for _ in range(MAX_PAGES):
        response = client.get_author_feed(
            actor=actor, cursor=cursor, filter="posts_no_replies", limit=PAGE_SIZE
        )
        reached_since = False
        for item in response.feed:
            if item.reason is not None:  # A repost of someone else's post.
                continue
            record = item.post.record
            created_at = datetime.fromisoformat(record.created_at)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at < since:
                reached_since = True
                continue
            posts += 1
            keys.update(keys_in_record(record, created_at))
        cursor = response.cursor
        if reached_since or not cursor or not response.feed:
            break
    else:
        logger.warning(f"Stopped reading post history after {MAX_PAGES} pages")
    logger.info(f"Read {len(keys)} keys from {posts} posts by {actor} since {since}")
    return keys


def public_published_keys(actor: str, since: datetime | None = None) -> set[str]:
    """``published_keys`` read from the public AppView, without logging in."""
    return published_keys(Client(base_url=PUBLIC_APPVIEW), actor, since)


class SeenPosts:
    """Keys (see ``post_key``) of posts already made, by the account or in this run."""

    def __init__(self, keys: Iterable[str] = ()) -> None:
        self.keys = set(keys)

    @staticmethod
    def key(post) -> str | None:
        link = getattr(post, "link", None)
        if link is None:
            return None
        embed = getattr(post, "embed", None)
        if embed is not None:
            title = embed.external.title
        else:
            title = getattr(post, "title", None)
        key = post_key(link, title or "")
        day = getattr(post, "day", None)
        return daily_key(key, day) if day is not None else key

    def is_new(self, post) -> bool:
        key = self.key(post)
        return key is None or key not in self.keys

    def add(self, post) -> None:
        key = self.key(post)
        if key is not None:
            self.keys.add(key)
