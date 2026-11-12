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

Follow-ups (a meeting's reminder, later its recording or minutes) are replies
threaded under the post they follow up. A reply is keyed by the post it
replies to (its thread root's URI) and its kind, e.g. ``reminder``. Later runs
tell a reply's kind from its text, which starts with that kind's fixed prefix
(``REPLY_PREFIXES``). A reply with a link card is also keyed by the card's
link, so what it links isn't posted again on its own (e.g. minutes posted
standalone once the meeting post is older than the history). The history also maps each key to the post's ``uri``/``cid``
(``PostRef``), so a later run can find the post to reply to.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from atproto import Client, models

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

# Items have come back as much as five days after first being posted.
HISTORY = timedelta(days=30)
PAGE_SIZE = 100
MAX_PAGES = 20

PUBLIC_APPVIEW = "https://public.api.bsky.app/xrpc"

EASTERN = ZoneInfo("America/New_York")

CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)

# The text each kind of reply starts with, so later runs can tell a reply's
# kind from its text alone. A new kind of follow-up registers its prefix here
# (e.g. "summary": "AI summary of the agenda"), and its posts' text must start
# with it.
REPLY_PREFIXES: dict[str, str] = {
    "reminder": "Starting at",
    "minutes": "Minutes posted",
    "recording": "Recording",
    "summary": "AI summary of the agenda",
}

# The weekly look-ahead's day-by-day replies (see ``lookahead``): one kind per
# weekday and part, e.g. "lookahead-monday" ("Monday, Nov 16 ...") and, when a
# busy day is split, "lookahead-monday-2" ("Monday (part 2), Nov 16 ...").
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
LOOKAHEAD_MAX_PARTS = 4


def lookahead_kind(weekday: int, part: int = 1) -> str:
    """Reply kind of part ``part`` of the look-ahead's ``weekday`` (0 = Monday)."""
    kind = f"lookahead-{WEEKDAYS[weekday].lower()}"
    return kind if part == 1 else f"{kind}-{part}"


for _weekday, _name in enumerate(WEEKDAYS):
    REPLY_PREFIXES[lookahead_kind(_weekday)] = f"{_name}, "
    for _part in range(2, LOOKAHEAD_MAX_PARTS + 1):
        REPLY_PREFIXES[lookahead_kind(_weekday, _part)] = f"{_name} (part {_part}), "


@dataclass(frozen=True)
class PostRef:
    """A post on Bluesky: what a reply needs to point at it."""

    uri: str
    cid: str

    def strong_ref(self) -> models.ComAtprotoRepoStrongRef.Main:
        return models.ComAtprotoRepoStrongRef.Main(uri=self.uri, cid=self.cid)


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


def reply_key(root_uri: str, kind: str) -> str:
    """Key of the ``kind`` follow-up (e.g. ``reminder``) to the thread at ``root_uri``."""
    return f"{root_uri} {kind}"


def reply_kind(text: str) -> str | None:
    """The kind of follow-up a reply's text is, by its prefix (``REPLY_PREFIXES``)."""
    for kind, prefix in REPLY_PREFIXES.items():
        if text.startswith(prefix):
            return kind
    return None


def keys_in_record(record, posted: datetime | None = None) -> set[str]:
    """Keys of what a post record points at: its link card and any links in its text.

    Each link also gets a daily key for the Eastern day the record was posted
    (``posted``), using the card's title, or for links in the text the text,
    to tell whether it is a cancellation.

    A reply is keyed by its thread and kind (see ``reply_key``) and its
    card's link, if it has one; replies whose text doesn't start with a known
    prefix (written by hand) have no keys.
    """
    external = getattr(getattr(record, "embed", None), "external", None)
    reply = getattr(record, "reply", None)
    if reply is not None:
        kind = reply_kind(record.text or "")
        if kind is None:
            return set()
        keys = {reply_key(reply.root.uri, kind)}
        if external is not None:
            keys.add(post_key(external.uri, external.title or ""))
        return keys
    keys = set()
    day = posted.astimezone(EASTERN).date() if posted is not None else None
    if external is not None:
        key = post_key(external.uri, external.title or "")
        keys.add(key)
        if day is not None:
            keys.add(daily_key(key, day))
    for facet in getattr(record, "facets", None) or []:
        for feature in facet.features:
            uri = getattr(feature, "uri", None)
            if uri:
                keys.add(post_key(uri))
                if day is not None:
                    keys.add(daily_key(post_key(uri, record.text or ""), day))
    return keys


def published_posts(
    client: Client, actor: str, since: datetime | None = None
) -> dict[str, PostRef]:
    """Keys of what ``actor`` has posted since ``since`` (default: 30 days ago),
    each mapped to the most recent post with that key.

    Raises whatever the client raises: without the history the run can't tell
    what is new, so callers should not post.
    """
    since = since or datetime.now(UTC) - HISTORY
    keys: dict[str, PostRef] = {}
    cursor = None
    posts = 0
    for _ in range(MAX_PAGES):
        response = client.get_author_feed(
            actor=actor, cursor=cursor, filter="posts_with_replies", limit=PAGE_SIZE
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
            ref = PostRef(item.post.uri, item.post.cid)
            # The feed is newest first: keep the latest post for each key.
            for key in keys_in_record(record, created_at):
                keys.setdefault(key, ref)
        cursor = response.cursor
        if reached_since or not cursor or not response.feed:
            break
    else:
        logger.warning(f"Stopped reading post history after {MAX_PAGES} pages")
    logger.info(f"Read {len(keys)} keys from {posts} posts by {actor} since {since}")
    return keys


def published_keys(
    client: Client, actor: str, since: datetime | None = None
) -> set[str]:
    """Just the keys of ``published_posts``."""
    return set(published_posts(client, actor, since))


def public_published_posts(
    actor: str, since: datetime | None = None
) -> dict[str, PostRef]:
    """``published_posts`` read from the public AppView, without logging in."""
    return published_posts(Client(base_url=PUBLIC_APPVIEW), actor, since)


class SeenPosts:
    """Keys (see ``post_key``) of posts already made, by the account or in this run.

    Built from ``published_posts``, it also knows which post each key refers
    to, so replies can find the post they follow up (``ref``).
    """

    def __init__(self, keys: Iterable[str] | Mapping[str, PostRef] = ()) -> None:
        self.refs: dict[str, PostRef] = dict(keys) if isinstance(keys, Mapping) else {}
        self.keys = set(keys)
        # Keys of the posts sent this run (see ``add``).
        self.added: set[str] = set()

    @staticmethod
    def key(post) -> str | None:
        kind = getattr(post, "reply_kind", None)
        if kind is not None:
            reply = getattr(post, "reply", None)
            return reply_key(reply.root.uri, kind) if reply is not None else None
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

    def ref(self, target) -> PostRef | None:
        """The post made for ``target``: a key, or a ``Post`` sent earlier."""
        key = target if isinstance(target, str) else self.key(target)
        return self.refs.get(key) if key is not None else None

    def resolve_reply(self, post) -> bool:
        """Point a reply at the posts it names by key (``Post.replying_to``).

        Returns False if a post it replies to can't be found, e.g. the
        morning's meeting post failed or is older than the history.
        """
        root_target = getattr(post, "reply_root", None)
        if root_target is None or getattr(post, "reply", None) is not None:
            return True
        root = self.ref(root_target)
        parent_target = getattr(post, "reply_parent", None)
        parent = root if parent_target is None else self.ref(parent_target)
        if root is None or parent is None:
            return False
        post.reply_to(root, parent)
        return True

    def added_this_run(self, key: str) -> bool:
        """Whether the post with ``key`` was sent this run (not just in the history)."""
        return key in self.added

    def is_new(self, post) -> bool:
        key = self.key(post)
        return key is None or key not in self.keys

    def add(self, post, ref: PostRef | None = None) -> None:
        key = self.key(post)
        if key is not None:
            self.keys.add(key)
            self.added.add(key)
            if ref is not None:
                self.refs[key] = ref
