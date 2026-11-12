"""Norwalk Oyster: its community event calendar and its newsletter.

Neither has a usable RSS feed. The site's calendar is a JSON file its pages
load, and the beehiiv newsletter's feed sits behind a bot check, so issues are
read from the newsletter's Google News sitemap, which lists each recent issue
with its title and publish time.
"""

import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")

SITE_URL = "https://norwalkoyster.com"
EVENTS_URL = f"{SITE_URL}/directory/norwalk-city-directory/project/data.json"
NEWSLETTER_SITEMAP_URL = "https://newsletter.norwalkoyster.com/sitemap.xml"

# The site's own "Top-rated events" list shows events scored 4 or 5 out of 5.
MIN_QUALITY_SCORE = 4

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT = 30.0
USER_AGENT = "rss-post (Norwalk events Bluesky bot)"

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


@dataclass(frozen=True)
class Event:
    name: str
    url: str
    day: date
    time: str = ""
    end_time: str = ""
    venue: str = ""
    description: str = ""

    @property
    def when(self) -> str:
        if self.time and self.end_time:
            return f"{self.time} – {self.end_time}"
        return self.time

    @property
    def starts_at(self) -> datetime | None:
        """The start time in Eastern, or ``None`` if the time is missing or
        ambiguous (the calendar has times like ``7:00`` with no AM/PM)."""
        try:
            start = datetime.strptime(self.time, "%I:%M %p")
        except ValueError:
            return None
        return datetime.combine(self.day, start.time(), tzinfo=EASTERN)


@dataclass(frozen=True)
class Issue:
    title: str
    url: str
    published: datetime


def fetch(url: str) -> httpx.Response:
    """GET ``url``, retrying network failures, rate limits and 5xx responses.

    Raises ``httpx.HTTPError`` if it still fails, so a broken source is
    reported rather than looking like a day with nothing new.
    """
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = httpx.get(
                url,
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as e:
            transient = not isinstance(e, httpx.HTTPStatusError) or (
                e.response.status_code == 429 or e.response.status_code >= 500
            )
            if attempt == ATTEMPTS or not transient:
                raise
            delay = BACKOFF_SECONDS * attempt
            logger.warning(
                f"Attempt {attempt}/{ATTEMPTS} to fetch {url} failed ({e}); "
                f"retrying in {delay}s"
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def parse_day(value: str) -> date | None:
    """The calendar's ``M/D/YYYY`` dates (zero padding varies)."""
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y").date()
    except (AttributeError, ValueError):
        return None


def events_on(data: dict, day: date) -> list[Event]:
    """Top-rated events from the calendar data that happen on ``day``."""
    events = []
    for raw in data.get("events", []):
        event_day = parse_day(raw.get("date", ""))
        if event_day is None:
            logger.warning(
                f"Skipping Norwalk Oyster event with unreadable date "
                f"{raw.get('date')!r}: {raw.get('name')}"
            )
            continue
        if event_day != day:
            continue
        if (raw.get("qualityScore") or 0) < MIN_QUALITY_SCORE:
            continue
        if raw.get("recurring") == "Dupe" or not raw.get("slug"):
            continue
        events.append(
            Event(
                name=raw["name"].strip(),
                url=f"{SITE_URL}/directory/event/{quote(raw['slug'], safe='')}",
                day=event_day,
                time=(raw.get("time") or "").strip(),
                end_time=(raw.get("endTime") or "").strip(),
                venue=(raw.get("venue") or "").strip(),
                description=(raw.get("short") or "").strip(),
            )
        )
    return events


def get_events_on(day: date) -> list[Event]:
    logger.info(f"Reading Norwalk Oyster events for {day}")
    events = events_on(fetch(EVENTS_URL).json(), day)
    logger.info(f"Found {len(events)} top-rated Norwalk Oyster events on {day}")
    return events


def get_events_by_day(days: Iterable[date]) -> dict[date, list[Event]]:
    """Top-rated events on each of ``days``, from one read of the calendar."""
    days = list(days)
    logger.info(f"Reading Norwalk Oyster events for {days[0]} to {days[-1]}")
    data = fetch(EVENTS_URL).json()
    return {day: events_on(data, day) for day in days}


def issues_from_sitemap(xml: bytes) -> list[Issue]:
    """Newsletter issues listed in a Google News sitemap.

    Only entries with ``news:`` metadata are issues; the rest are site pages
    (archive, authors, ...) or issues too old for the news sitemap.
    """
    issues = []
    for url in ET.fromstring(xml).iterfind("sm:url", SITEMAP_NS):
        loc = url.findtext("sm:loc", "", SITEMAP_NS).strip()
        title = url.findtext("news:news/news:title", "", SITEMAP_NS).strip()
        published = url.findtext(
            "news:news/news:publication_date", "", SITEMAP_NS
        ).strip()
        if not (loc and title and published):
            continue
        try:
            published_at = datetime.fromisoformat(published)
        except ValueError:
            logger.warning(f"Skipping issue with unreadable date {published!r}: {loc}")
            continue
        if published_at.tzinfo is None:
            logger.warning(f"Skipping issue without a time zone {published!r}: {loc}")
            continue
        issues.append(Issue(title=title, url=loc, published=published_at))
    return issues


def get_newsletter_issues() -> list[Issue]:
    logger.info(f"Reading Norwalk Oyster newsletter from {NEWSLETTER_SITEMAP_URL}")
    issues = issues_from_sitemap(fetch(NEWSLETTER_SITEMAP_URL).content)
    logger.info(f"Found {len(issues)} Norwalk Oyster newsletter issues")
    return issues
