"""Norwalk Board of Education meetings from the school district's calendar.

The Board's meetings aren't in the city's CivicClerk tenant. Norwalk Public
Schools lists them on its Finalsite "BOE Calendar", which publishes an iCal
feed of upcoming meetings. Agendas are on BoardDocs, which refuses automated
requests, so posts link to it rather than reading it.

The feed has no page per meeting, so each meeting's card links to the
calendar page with Finalsite's ``~occur_id`` deep link for that meeting, which
also gives every meeting a link of its own to be remembered by. Like city
meetings, each is posted the day before and again on the day.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")

SITE_URL = "https://www.norwalkps.org"
CALENDAR_PAGE_URL = f"{SITE_URL}/board-of-education1/boe-calendar"
# The feed the calendar page's "subscribe" button offers for its calendar.
CALENDAR_ICS_URL = f"{SITE_URL}/calendar/calendar_370.ics"
AGENDAS_URL = "https://go.boarddocs.com/ct/norwalkps/Board.nsf/Public"

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT = 30.0
USER_AGENT = "rss-post (Norwalk meetings Bluesky bot)"

ZOOM_URL = re.compile(r"https://[\w.-]*zoom\.us/\S+")
# Kept in capitals when the calendar's all-caps titles are title-cased.
ACRONYMS = {"BOE", "ELL", "IEP", "MLL", "NPS", "PTO", "SPED"}
ICAL_ESCAPES = re.compile(r"\\([\\;,nN])")


@dataclass(frozen=True)
class Meeting:
    title: str
    url: str
    starts_at: datetime
    location: str = ""
    zoom: str = ""

    @property
    def when(self) -> str:
        """E.g. ``Nov 17, 2026 at 7:00 PM EST``, like city meetings."""
        hour = self.starts_at.hour % 12 or 12
        return (
            f"{self.starts_at:%b} {self.starts_at.day}, {self.starts_at.year} "
            f"at {hour}:{self.starts_at:%M %p} {self.starts_at.tzname()}"
        )


def fetch(url: str) -> httpx.Response:
    """GET ``url``, retrying network failures, rate limits and 5xx responses.

    Raises ``httpx.HTTPError`` if it still fails, so a broken source is
    reported rather than looking like a day with no meetings.
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


def unescape(value: str) -> str:
    return ICAL_ESCAPES.sub(
        lambda m: "\n" if m.group(1) in "nN" else m.group(1), value
    ).strip()


def parse_events(ics: str) -> list[dict[str, tuple[dict[str, str], str]]]:
    """The ``VEVENT``s in an iCal file, as ``{NAME: (params, raw value)}``."""
    # Long lines are folded onto continuation lines starting with a space or tab.
    lines = re.sub(r"\r?\n[ \t]", "", ics).splitlines()
    events, event = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            event = {}
        elif line == "END:VEVENT":
            if event is not None:
                events.append(event)
            event = None
        elif event is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            event[name.upper()] = (
                dict(p.split("=", 1) for p in params if "=" in p),
                value,
            )
    return events


def parse_start(params: dict[str, str], value: str) -> datetime | None:
    """A ``DTSTART`` in Eastern; ``None`` for all-day or unreadable values.

    The district's feed uses floating local times, which are Eastern.
    """
    try:
        if value.endswith("Z"):
            start = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return start.replace(tzinfo=UTC).astimezone(EASTERN)
        start = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    tz = ZoneInfo(params["TZID"]) if "TZID" in params else EASTERN
    return start.replace(tzinfo=tz).astimezone(EASTERN)


def title_case(title: str) -> str:
    """``SPED CURRICULUM SUBCOMMITTEE`` -> ``SPED Curriculum Subcommittee``."""
    if title != title.upper():
        return title
    return " ".join(
        word if word.strip("/&-") in ACRONYMS else word.title()
        for word in title.split()
    )


def meeting_from_event(event: dict) -> Meeting | None:
    summary = unescape(event.get("SUMMARY", ({}, ""))[1])
    uid = event.get("UID", ({}, ""))[1].split("@", 1)[0].strip()
    starts_at = parse_start(*event.get("DTSTART", ({}, "")))
    if not (summary and uid and starts_at):
        logger.warning(f"Skipping unreadable Board of Education event: {event}")
        return None
    location = unescape(event.get("LOCATION", ({}, ""))[1])
    description = unescape(event.get("DESCRIPTION", ({}, ""))[1])
    zoom = ZOOM_URL.search(f"{location}\n{description}")
    if ZOOM_URL.fullmatch(location):
        location = ""  # Online only; the Zoom link is posted separately.
    return Meeting(
        title=title_case(summary),
        url=f"{CALENDAR_PAGE_URL}/~occur_id/{uid}",
        starts_at=starts_at,
        location=location,
        zoom=zoom.group(0) if zoom else "",
    )


def meetings_on(ics: str, *days: date) -> list[Meeting]:
    """The calendar's meetings that start on any of ``days`` (Eastern), in
    time order."""
    meetings = [
        meeting
        for meeting in map(meeting_from_event, parse_events(ics))
        if meeting is not None and meeting.starts_at.date() in days
    ]
    return sorted(meetings, key=lambda meeting: meeting.starts_at)


def get_meetings_on(*days: date) -> list[Meeting]:
    """Meetings on any of ``days``, from one read of the calendar."""
    on = " and ".join(map(str, days))
    logger.info(f"Reading Board of Education meetings for {on}")
    meetings = meetings_on(fetch(CALENDAR_ICS_URL).text, *days)
    for meeting in meetings:
        logger.info(f"Found Board of Education meeting: {meeting.title} {meeting.when}")
    logger.info(f"Found {len(meetings)} Board of Education meetings on {on}")
    return meetings


if __name__ == "__main__":
    today = datetime.now(EASTERN).date()
    for meeting in get_meetings_on(today, today + timedelta(days=1)):
        print(f"{meeting.title}\n{meeting.when}\n{meeting.url}\n{meeting.zoom}\n")
