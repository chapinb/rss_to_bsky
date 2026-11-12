"""Community events from the City of Norwalk's own calendars.

The city's CivicPlus site publishes an RSS feed per calendar category. Unlike
the meetings calendar (posted as items are published), these are posted on
the day of the event: each item carries the event's date, times and location
in ``calendarEvent:`` elements and an HTML description.

Feeds only list upcoming events, a couple of weeks ahead.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from rss_post.logging_config import get_logger
from rss_post.norwalk_oyster import fetch

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")

FEED_URL = "https://www.norwalkct.gov/RSSFeed.aspx?ModID=58&CID={}"
REC_PARKS_URL = FEED_URL.format("Recreation-Parks-40")
MAIN_CITY_URL = FEED_URL.format("Main-City-Calendar-14")

NS = {"cal": "https://www.norwalkct.gov/Calendar.aspx"}

# The calendar marks "no end time" as 11:59 PM and "all day" as 12:00 AM - 11:59 PM.
END_OF_DAY = time(23, 59)

# Lines of a location that only name the city, e.g. "Norwalk, CT 06854".
CITY_LINE = re.compile(r"^Norwalk,?\s+CT\b", re.IGNORECASE)


@dataclass(frozen=True)
class Event:
    name: str
    url: str
    day: date
    start: time | None = None
    end: time | None = None
    place: str = ""
    image: str | None = None

    @property
    def when(self) -> str:
        if self.start is None:
            return ""
        if self.end is None:
            return clock(self.start)
        return f"{clock(self.start)} – {clock(self.end)}"

    @property
    def starts_at(self) -> datetime | None:
        """The start time in Eastern, or ``None`` for an all-day event."""
        if self.start is None:
            return None
        return datetime.combine(self.day, self.start, tzinfo=EASTERN)


def clock(t: time) -> str:
    """``10:00 AM``, ``1:00 PM`` (no leading zero)."""
    return t.strftime("%I:%M %p").lstrip("0")


def parse_start_day(text: str) -> date | None:
    """The first day of ``November 14, 2026`` or ``October 7, 2026 - January 3, 2027``."""
    try:
        return datetime.strptime(text.split(" - ")[0].strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def parse_times(text: str) -> tuple[time | None, time | None]:
    """Start and end of ``10:00 AM - 12:30 PM``; ``None`` where not given."""
    parts = [p.strip() for p in text.split(" - ")]
    try:
        times = [datetime.strptime(p, "%I:%M %p").time() for p in parts if p]
    except ValueError:
        return None, None
    if not times:
        return None, None
    start = times[0]
    end = times[1] if len(times) > 1 else None
    if end == END_OF_DAY:
        end = None
        if start == time(0, 0):
            start = None
    return start, end


def place_from_description(html: str) -> str:
    """The location lines of the item's description, without the city line.

    The ``calendarEvent:Location`` element runs lines together ("328 Flax hill
    rd.Norwalk, CT"), so the description's ``<br>``-separated copy is used.
    """
    _, found, location = html.partition("Location:")
    if not found:
        return ""
    lines = BeautifulSoup(location, "html.parser").stripped_strings
    return ", ".join(line for line in lines if not CITY_LINE.match(line))


def events_from_feed(xml: bytes) -> list[Event]:
    events = []
    for item in ET.fromstring(xml).iter("item"):
        name = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        dates = item.findtext("cal:EventDates", "", NS)
        day = parse_start_day(dates)
        if not (name and url) or day is None:
            logger.warning(f"Skipping city calendar item with no date {dates!r}: {url}")
            continue
        start, end = parse_times(item.findtext("cal:EventTimes", "", NS))
        enclosure = item.find("enclosure")
        image = None
        if enclosure is not None and enclosure.get("type", "").startswith("image/"):
            image = enclosure.get("url") or None
        events.append(
            Event(
                name=" ".join(name.split()),
                url=url,
                day=day,
                start=start,
                end=end,
                place=place_from_description(item.findtext("description") or ""),
                image=image,
            )
        )
    return events


def events_on(xml: bytes, day: date) -> list[Event]:
    """Events starting on ``day``, all-day ones first, then by start time."""
    events = [e for e in events_from_feed(xml) if e.day == day]
    return sorted(events, key=lambda e: e.start or time.min)


def get_events_on(feed_url: str, day: date) -> list[Event]:
    logger.info(f"Reading city calendar events for {day} from {feed_url}")
    events = events_on(fetch(feed_url).content, day)
    logger.info(f"Found {len(events)} city calendar events on {day}")
    return events


def _words(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def same_event(name: str, other: str) -> bool:
    """Whether two listings' names look like the same event.

    Catches the same event listed as "Meet the Mayor" on one calendar and
    "Meet the Mayor at Odeens" on another.
    """
    a, b = _words(name), _words(other)
    shorter, longer = sorted((a, b), key=len)
    return a == b or (len(shorter) >= 12 and shorter in longer)
