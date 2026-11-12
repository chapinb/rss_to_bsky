"""Skip items the bot has already posted, using its own Bluesky posts as the record.

Feeds re-surface items the bot has already posted: the city feeds label EDT
times as ``-0500`` (items look an hour in the future and pass two hourly
windows), edited items get a new ``pubDate``, and live stories are updated in
place. Rather than chase each timestamp quirk, every run reads the links the
account has posted recently and skips any item whose link is among them.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from atproto import Client

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

# Items have come back as much as five days after first being posted.
HISTORY = timedelta(days=30)
PAGE_SIZE = 100
MAX_PAGES = 20

PUBLIC_APPVIEW = "https://public.api.bsky.app/xrpc"


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


def links_in_record(record) -> set[str]:
    """Links a post record points at: its link card and any links in its text."""
    links = set()
    external = getattr(getattr(record, "embed", None), "external", None)
    if external is not None:
        links.add(external.uri)
    for facet in getattr(record, "facets", None) or []:
        for feature in facet.features:
            uri = getattr(feature, "uri", None)
            if uri:
                links.add(uri)
    return links


def published_links(
    client: Client, actor: str, since: datetime | None = None
) -> set[str]:
    """Normalized links ``actor`` has posted since ``since`` (default: 30 days ago).

    Raises whatever the client raises: without the history the run can't tell
    what is new, so callers should not post.
    """
    since = since or datetime.now(UTC) - HISTORY
    links: set[str] = set()
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
            links.update(normalize_link(link) for link in links_in_record(record))
        cursor = response.cursor
        if reached_since or not cursor or not response.feed:
            break
    else:
        logger.warning(f"Stopped reading post history after {MAX_PAGES} pages")
    logger.info(f"Read {len(links)} links from {posts} posts by {actor} since {since}")
    return links


def public_published_links(actor: str, since: datetime | None = None) -> set[str]:
    """``published_links`` read from the public AppView, without logging in."""
    return published_links(Client(base_url=PUBLIC_APPVIEW), actor, since)


class SeenLinks:
    """Links already posted, by the account or earlier in this run."""

    def __init__(self, links: Iterable[str] = ()) -> None:
        self.links = {normalize_link(link) for link in links}

    def is_new(self, post) -> bool:
        link = getattr(post, "link", None)
        return link is None or normalize_link(link) not in self.links

    def add(self, post) -> None:
        link = getattr(post, "link", None)
        if link is not None:
            self.links.add(normalize_link(link))
