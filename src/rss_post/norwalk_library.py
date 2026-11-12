"""Norwalk Public Library events, from its LibraryCalendar JSON feed.

The feed lists every upcoming event at the main library and the South Norwalk
branch, many of them weekly storytimes and children's programs. Only a few
community and adult programs a day are worth a post, so the feed is filtered:

- Events for children only (every age group a children's one) are skipped;
  events marked "Adults" or "Everyone", or with no age group, are kept.
- Storytimes and play groups are skipped whatever their age group.
- Recurring series (a title that appears more than ``MAX_SERIES_EVENTS``
  times in the feed, such as weekly clubs or drop-in help sessions) are
  skipped; they would otherwise be posted every week.
- Closure notices ("Library Closed", early closing) and cancelled events are
  skipped.
- Room bookings by outside groups (a bare listing: no program type, no
  description and no image, such as "Chamber of Commerce" or "Norwalk
  Aquarium Meeting") are skipped. Library programs always have at least one
  of these, usually all three.
- Repeat sessions of a program on the same day get one post, for the first.
- At most ``MAX_EVENTS_PER_DAY`` remain, chosen featured events first, then by
  start time.
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from rss_post.logging_config import get_logger
from rss_post.norwalk_oyster import fetch

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")

SITE_URL = "https://norwalkpl.librarycalendar.com"
EVENTS_URL = f"{SITE_URL}/events/feed/json"

CHILDREN_AGE_GROUPS = frozenset(
    {
        "Babies",
        "Toddlers",
        "Preschool Age",
        "Elementary-aged Children",
        "Tweens/Teens",
    }
)
SKIPPED_PROGRAM_TYPES = frozenset({"Storytime", "Play Group"})
MAX_SERIES_EVENTS = 3
MAX_EVENTS_PER_DAY = 3

_NOT_AN_EVENT = re.compile(r"\b(closed|closing|cancel+ed)\b", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]*>|&nbsp;")


@dataclass(frozen=True)
class Event:
    title: str
    url: str
    starts_at: datetime
    ends_at: datetime | None = None
    location: str = ""
    description: str = ""
    image: str | None = None
    featured: bool = False

    @property
    def when(self) -> str:
        start = _clock(self.starts_at)
        if self.ends_at and self.ends_at > self.starts_at:
            return f"{start} – {_clock(self.ends_at)}"
        return start


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def _names(field) -> set[str]:
    """Term names from a taxonomy field: ``{"53": "Storytime"}`` or ``[]``."""
    return set(field.values()) if isinstance(field, dict) else set()


def _series_key(title: str) -> str:
    return " ".join(title.lower().split())


def _parse_time(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None


def _location(raw: dict) -> str:
    rooms = sorted(_names(raw.get("room")))
    branches = sorted(_names(raw.get("branch")))
    if raw.get("online_address") and not (rooms or branches):
        return "Online"
    return ", ".join(rooms[:1] + branches[:1])


def is_room_booking(raw: dict, program_types: set[str]) -> bool:
    """Whether an event is an outside group's room booking, not a program.

    Bookings are entered as a bare listing: a title and a room, with no
    program type, description or image.
    """
    description = _TAGS.sub("", raw.get("description") or "").strip()
    return not (program_types or description or raw.get("image"))


def is_notable(raw: dict, series_counts: Counter) -> bool:
    """Whether an event is a community or adult program worth a post."""
    title = raw.get("title") or ""
    if not (raw.get("public", True) and raw.get("published", True)):
        return False
    if raw.get("moderation_state", "published") != "published":
        return False
    if _NOT_AN_EVENT.search(title):
        return False
    ages = _names(raw.get("age_group"))
    if ages and ages <= CHILDREN_AGE_GROUPS:
        return False
    program_types = _names(raw.get("program_type"))
    if program_types & SKIPPED_PROGRAM_TYPES:
        return False
    if is_room_booking(raw, program_types):
        return False
    return series_counts[_series_key(title)] <= MAX_SERIES_EVENTS


def events_on(data: list[dict], day: date) -> list[Event]:
    """The notable events on ``day``, capped at ``MAX_EVENTS_PER_DAY``.

    The cap is applied to the whole day, before events that have started are
    dropped, so every run that day picks the same events and the cap holds
    across runs.
    """
    series_counts = Counter(_series_key(raw.get("title") or "") for raw in data)
    events = []
    for raw in data:
        tz = ZoneInfo(raw.get("timezone") or "America/New_York")
        starts_at = _parse_time(raw.get("start_date"), tz)
        if starts_at is None or not raw.get("url") or not raw.get("title"):
            logger.warning(
                f"Skipping library event without a start time or link: "
                f"{raw.get('title')!r}"
            )
            continue
        if starts_at.astimezone(EASTERN).date() != day:
            continue
        if not is_notable(raw, series_counts):
            continue
        events.append(
            Event(
                title=raw["title"].strip(),
                url=raw["url"],
                starts_at=starts_at,
                ends_at=_parse_time(raw.get("end_date"), tz),
                location=_location(raw),
                description=raw.get("description") or "",
                image=raw.get("image") or None,
                featured=bool(raw.get("featured")),
            )
        )
    events.sort(key=lambda e: (not e.featured, e.starts_at))
    # Repeat sessions of one program on the same day get one post.
    first_sessions = {}
    for event in events:
        first_sessions.setdefault(_series_key(event.title), event)
    events = list(first_sessions.values())
    if len(events) > MAX_EVENTS_PER_DAY:
        logger.info(
            f"Capping {len(events)} library events on {day} at {MAX_EVENTS_PER_DAY}"
        )
    return sorted(events[:MAX_EVENTS_PER_DAY], key=lambda e: e.starts_at)


def get_events_on(day: date) -> list[Event]:
    logger.info(f"Reading Norwalk Public Library events for {day}")
    events = events_on(fetch(EVENTS_URL).json(), day)
    logger.info(f"Found {len(events)} notable Norwalk Public Library events on {day}")
    return events
