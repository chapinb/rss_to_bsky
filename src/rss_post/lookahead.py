"""The weekly look-ahead: a Sunday-evening thread of the week ahead.

The root post counts the coming week's (Monday to Sunday) meetings and events;
replies under it list them day by day, in time order, each reply continuing
the chain (its parent is the reply before it).

One reply per day rather than one per item: a busy week has 20-40 items, and
a 40-post thread is hard to read and slow to post, while a day's items fit in
one reply (``REPLY_MAX_LENGTH``) most days. A busy day is split into
"Monday (part 2), ..." replies; each item's name links to its page.

Dedup (see ``dedup``):

- The root links the city's meeting portal and is keyed by that link and the
  day it is posted (``Post.keyed_by``), the same daily key meeting posts use.
  The look-ahead only goes out on Sundays, so that key is once per week: the
  hourly runs after the first one find the root in the account's history.
- Each day's reply is a reply kind of its own (``dedup.lookahead_kind``, text
  prefixes in ``REPLY_PREFIXES``), keyed by the root's URI and the kind. A run
  that fails halfway is resumed by the next: replies already posted are found
  in the history (and still serve as the next reply's parent), and the rest
  continue the chain where it stopped.

Every source is read once for the whole week. A source that fails is logged
and left out; the thread goes out with what loaded, unless nothing did, in
which case the source is reported as failed and the next run tries again.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial

from rss_post import (
    norwalk_boe,
    norwalk_city_calendar,
    norwalk_civic_clerk,
    norwalk_library,
    norwalk_oyster,
)
from rss_post.dedup import (
    EASTERN,
    LOOKAHEAD_MAX_PARTS,
    WEEKDAYS,
    lookahead_kind,
)
from rss_post.logging_config import get_logger
from rss_post.post import Post

logger = get_logger(__name__)

# The look-ahead goes out from the first successful run at or after this hour
# (Eastern) on Sundays.
LOOKAHEAD_WEEKDAY = 6  # Sunday
LOOKAHEAD_HOUR = 18
# At most this many events a day in the thread (meetings are never capped);
# the rest are counted in a "+N more" line.
MAX_EVENTS_PER_DAY = 4
# Longer names are cut, so one item never takes over a reply.
MAX_NAME_LENGTH = 70
# Bluesky allows 300 graphemes of post text. Replies are measured in code
# points, which are never fewer than graphemes, with a small margin. Links are
# facets on the item names, so their URLs don't count.
REPLY_MAX_LENGTH = 295
# A city calendar event this close to a meeting with a matching name is a
# listing of that meeting.
SAME_MEETING_WINDOW = timedelta(minutes=30)
HASHTAG = "NorwalkCT"
# What the root post links to, and is keyed by.
ROOT_LINK = norwalk_civic_clerk.PORTAL_URL


@dataclass(frozen=True)
class Item:
    """A meeting or event on a day of the week ahead."""

    name: str
    url: str
    day: date
    starts_at: datetime | None = None
    # Shown when there is no usable start time (e.g. "7:00" with no AM/PM).
    time_text: str = ""
    meeting: bool = False

    @property
    def label(self) -> str:
        """``6 PM``, ``6:30 PM``, the source's own time text, or ``All day``."""
        if self.starts_at is not None:
            local = self.starts_at.astimezone(EASTERN)
            hour = local.hour % 12 or 12
            minutes = f":{local:%M}" if local.minute else ""
            return f"{hour}{minutes} {local:%p}"
        return self.time_text or "All day"

    @property
    def sort_key(self) -> datetime:
        """All-day and untimed items first, then by start time."""
        if self.starts_at is not None:
            return self.starts_at.astimezone(EASTERN)
        return datetime.combine(self.day, time.min, tzinfo=EASTERN)


Loader = Callable[[Sequence[date]], list[Item]]


def is_due(now: datetime) -> bool:
    """Whether a run at ``now`` should post this week's look-ahead.

    Every Sunday run from ``LOOKAHEAD_HOUR`` on; the root's key skips it after
    the first that succeeds, so a failed or missed 6 PM run is caught up.
    """
    local = now.astimezone(EASTERN)
    return local.weekday() == LOOKAHEAD_WEEKDAY and local.hour >= LOOKAHEAD_HOUR


def week_after(now: datetime) -> list[date]:
    """The seven days after ``now``'s (Eastern) day: Monday to Sunday on a Sunday."""
    today = now.astimezone(EASTERN).date()
    return [today + timedelta(days=n) for n in range(1, 8)]


def city_meetings(days: Sequence[date]) -> list[Item]:
    return [
        Item(
            name=meeting["title"],
            url=meeting["agenda"],
            day=meeting["starts_at"].date(),
            starts_at=meeting["starts_at"],
            meeting=True,
        )
        for meeting in norwalk_civic_clerk.get_meetings_between(days[0], days[-1])
    ]


def boe_meetings(days: Sequence[date]) -> list[Item]:
    return [
        Item(
            name=f"Board of Education: {meeting.title}",
            url=meeting.url,
            day=meeting.starts_at.date(),
            starts_at=meeting.starts_at,
            meeting=True,
        )
        for meeting in norwalk_boe.get_meetings_on(*days)
    ]


def oyster_events(days: Sequence[date]) -> list[Item]:
    return [
        Item(
            name=event.name,
            url=event.url,
            day=day,
            starts_at=event.starts_at,
            time_text=event.time,
        )
        for day, events in norwalk_oyster.get_events_by_day(days).items()
        for event in events
    ]


def library_events(days: Sequence[date]) -> list[Item]:
    return [
        Item(name=event.title, url=event.url, day=day, starts_at=event.starts_at)
        for day, events in norwalk_library.get_events_by_day(days).items()
        for event in events
    ]


def city_calendar_events(feed_url: str, days: Sequence[date]) -> list[Item]:
    return [
        Item(name=event.name, url=event.url, day=day, starts_at=event.starts_at)
        for day, events in norwalk_city_calendar.get_events_by_day(
            feed_url, days
        ).items()
        for event in events
    ]


# Events are chosen for a day's cap in this order: Norwalk Oyster's top-rated
# events first, as in the daily posts.
LOADERS: list[tuple[str, Loader]] = [
    ("city meetings", city_meetings),
    ("Board of Education meetings", boe_meetings),
    ("Norwalk Oyster events", oyster_events),
    ("Norwalk Public Library events", library_events),
    (
        "Recreation & Parks calendar events",
        partial(city_calendar_events, norwalk_city_calendar.REC_PARKS_URL),
    ),
    (
        "City of Norwalk calendar events",
        partial(city_calendar_events, norwalk_city_calendar.MAIN_CITY_URL),
    ),
]


@dataclass
class Day:
    """What the thread lists for a day: every meeting, and up to
    ``MAX_EVENTS_PER_DAY`` events (``more`` counts the rest)."""

    day: date
    items: list[Item]
    meetings: int = 0
    events: int = 0
    more: int = 0


def plan_days(items: list[Item], days: Sequence[date]) -> list[Day]:
    """Group ``items`` by day, dropping repeat listings and capping events.

    An event listed by two sources (by ``same_event``) counts once; the first
    source's listing is kept. An event that is a listing of a meeting that day
    (``is_meeting_listing``) is left to the meeting. Days with nothing on are
    left out.
    """
    planned = []
    for day in days:
        on_day = [item for item in items if item.day == day]
        meetings = [item for item in on_day if item.meeting]
        events: list[Item] = []
        for item in on_day:
            if item.meeting:
                continue
            if any(is_meeting_listing(item, m) for m in meetings) or any(
                norwalk_city_calendar.same_event(item.name, other.name)
                for other in meetings + events
            ):
                logger.info(
                    f"Weekly look-ahead: leaving out repeat listing {item.name}"
                )
                continue
            events.append(item)
        if not (meetings or events):
            continue
        shown = sorted(meetings + events[:MAX_EVENTS_PER_DAY], key=lambda i: i.sort_key)
        planned.append(
            Day(
                day,
                shown,
                meetings=len(meetings),
                events=len(events),
                more=max(0, len(events) - MAX_EVENTS_PER_DAY),
            )
        )
    return planned


# Words that say what kind of session a meeting is, or when, rather than
# whose it is: "Special Meeting Recreation, Parks & Cultural Affairs Nov.18
# 2026" and "Recreation, Parks and Cultural Affairs Public Hearing" are both
# the Recreation, Parks and Cultural Affairs committee's.
NAME_NOISE = frozenset(
    [
        "a",
        "an",
        "and",
        "at",
        "for",
        "of",
        "on",
        "the",
        "to",
        "meeting",
        "meetings",
        "special",
        "regular",
        "public",
        "hearing",
        "hearings",
        "session",
        "workshop",
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
)


def name_words(name: str) -> frozenset[str]:
    """The words that say whose meeting ``name`` is (no dates or noise words)."""
    words = re.findall(r"[a-z]+", name.lower().replace("&", " and "))
    return frozenset(word for word in words if word not in NAME_NOISE)


def similar_names(name: str, other: str) -> bool:
    """Whether two names share most of their meaningful words.

    "Recreation & Parks Committee" and "Recreation and Parks Committee Special
    Meeting" match; "Planning Commission" and "Zoning Commission" don't.
    """
    a, b = name_words(name), name_words(other)
    if not (a and b):
        return False
    shared = len(a & b)
    return shared >= 2 and shared / len(a | b) >= 0.75


def is_meeting_listing(event: Item, meeting: Item) -> bool:
    """Whether ``event`` (e.g. from a city calendar) lists ``meeting``: a
    similar name and a start within ``SAME_MEETING_WINDOW``."""
    if event.starts_at is None or meeting.starts_at is None:
        return False
    return abs(
        event.starts_at - meeting.starts_at
    ) <= SAME_MEETING_WINDOW and similar_names(event.name, meeting.name)


# A date at the end of a name, e.g. "... Affairs Nov.18 2026": the reply's
# header already says the day.
TRAILING_DATE = re.compile(
    r"\s*\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s*"
    r"\d{1,2}(st|nd|rd|th)?,?\s*\d{4}$",
    re.IGNORECASE,
)


def tidy(name: str) -> str:
    """Collapse whitespace, fix spacing around punctuation and drop a trailing
    date: ``Recreation ,Parks ... Nov.18 2026`` -> ``Recreation, Parks ...``."""
    name = " ".join(name.split())
    name = TRAILING_DATE.sub("", name) or name
    name = re.sub(r"\s+([,.;:!?])", r"\1", name)
    return re.sub(r",(?=[^\s\d])", ", ", name)


def short_date(day: date) -> str:
    return f"{day:%b} {day.day}"


def header(day: date, part: int) -> str:
    """A reply's first line, which starts with its kind's prefix."""
    name = WEEKDAYS[day.weekday()]
    if part == 1:
        return f"{name}, {short_date(day)}"
    return f"{name} (part {part}), {short_date(day)}"


def item_name(item: Item) -> str:
    name = tidy(item.name)
    if len(name) > MAX_NAME_LENGTH:
        name = name[: MAX_NAME_LENGTH - 1].rsplit(" ", 1)[0] + "…"
    return name


def item_line(item: Item) -> str:
    return f"{item.label} · {item_name(item)}"


def more_line(more: int) -> str:
    return f"+{more} more"


def part_length(day: date, part: int, items: list[Item], more: int) -> int:
    """Length of a reply's text: its header, items and "+N more" line."""
    lines = [header(day, part)] + [item_line(item) for item in items]
    if more:
        lines.append(more_line(more))
    return len("\n".join(lines))


def split_day(day: Day) -> list[tuple[list[Item], int]]:
    """The day's items as reply parts that each fit ``REPLY_MAX_LENGTH``,
    each with the "+N more" count to end it with (only the last has one).

    Past ``LOOKAHEAD_MAX_PARTS`` parts, the rest are counted in "+N more".
    """
    parts: list[list[Item]] = [[]]
    for item in day.items:
        if parts[-1] and (
            part_length(day.day, len(parts), parts[-1] + [item], 0) > REPLY_MAX_LENGTH
        ):
            parts.append([])
        parts[-1].append(item)
    more = day.more + sum(len(part) for part in parts[LOOKAHEAD_MAX_PARTS:])
    parts = parts[:LOOKAHEAD_MAX_PARTS]
    # Make room for the "+N more" line: move the last item on to a part of
    # its own, or once there are as many parts as allowed, count it instead.
    while more and (
        part_length(day.day, len(parts), parts[-1], more) > REPLY_MAX_LENGTH
    ):
        item = parts[-1].pop()
        if len(parts) < LOOKAHEAD_MAX_PARTS:
            parts.append([item])
        else:
            more += 1
    return [(part, more if n == len(parts) - 1 else 0) for n, part in enumerate(parts)]


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def root_post(days: Sequence[date], planned: list[Day], posted_on: date) -> Post:
    meetings = sum(day.meetings for day in planned)
    events = sum(day.events for day in planned)
    return (
        Post()
        .with_text(
            f"This week in Norwalk: {plural(meetings, 'meeting')}, "
            f"{plural(events, 'event')}\n"
            f"{short_date(days[0])} – {short_date(days[-1])}, day by day in the "
            "replies.\n"
        )
        .with_link("City meeting calendar", ROOT_LINK)
        .with_text("\n")
        .with_tag(HASHTAG)
        .keyed_by(ROOT_LINK, posted_on)
    )


def day_posts(day: Day, root: Post, parent: Post | None) -> list[Post]:
    """The day's replies, continuing the chain from ``parent`` (or the root)."""
    posts = []
    for n, (items, more) in enumerate(split_day(day), start=1):
        post = Post().with_text(header(day.day, n))
        for item in items:
            post.with_text(f"\n{item.label} · ").with_link(item_name(item), item.url)
        if more:
            post.with_text("\n" + more_line(more))
        post.replying_to(root, lookahead_kind(day.day.weekday(), n), parent=parent)
        posts.append(post)
        parent = post
    return posts


def load_items(
    days: Sequence[date], loaders: Sequence[tuple[str, Loader]]
) -> list[Item]:
    """Every loader's items for ``days``, leaving out loaders that fail.

    Raises if every loader fails, so the run reports it and the next tries
    again, rather than posting an empty week.
    """
    items: list[Item] = []
    failed = []
    for name, load in loaders:
        try:
            loaded = load(days)
        except Exception:
            logger.exception(f"Weekly look-ahead: failed to read {name}; leaving out")
            failed.append(name)
            continue
        logger.info(f"Weekly look-ahead: read {len(loaded)} {name}")
        items.extend(loaded)
    if loaders and len(failed) == len(loaders):
        raise RuntimeError(f"Weekly look-ahead: every source failed ({failed})")
    return items


def weekly_lookahead(
    now: datetime | None = None,
    loaders: Sequence[tuple[str, Loader]] | None = None,
) -> list[Post]:
    """The look-ahead thread for the week after ``now``: the root, then one
    or more replies per day with something on, in order.

    Nothing if the week has nothing on.
    """
    now = now or datetime.now(EASTERN)
    days = week_after(now)
    logger.info(f"Building the weekly look-ahead for {days[0]} to {days[-1]}")
    items = load_items(days, LOADERS if loaders is None else loaders)
    planned = plan_days(items, days)
    if not planned:
        logger.info("Weekly look-ahead: nothing on this week; not posting")
        return []
    root = root_post(days, planned, now.astimezone(EASTERN).date())
    posts = [root]
    parent = None
    for day in planned:
        replies = day_posts(day, root, parent)
        posts.extend(replies)
        parent = replies[-1]
    logger.info(f"Weekly look-ahead: a root post and {len(posts) - 1} replies")
    return posts
