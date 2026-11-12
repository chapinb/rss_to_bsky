"""Today's and tomorrow's City of Norwalk meetings from CivicClerk.

Uses the public CivicClerk API via ``libmuni`` rather than scraping the
JavaScript-rendered portal. Meeting agendas are read to note where the public
can speak (public comment or a public hearing).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from libmuni.civicclerk import (
    Agenda,
    AgendaItem,
    APIStatusError,
    CivicClerkClient,
    CivicClerkError,
    Event,
    EventLocation,
    NetworkError,
)

from rss_post.dedup import CANCELLED
from rss_post.logging_config import get_logger

logger = get_logger(__name__)

TENANT = "norwalkct"
PORTAL_URL = f"https://{TENANT}.portal.civicclerk.com"
EASTERN = ZoneInfo("America/New_York")

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

T = TypeVar("T")


def is_transient(error: CivicClerkError) -> bool:
    """Whether retrying might help: network failures, rate limits and 5xx."""
    if isinstance(error, NetworkError):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code == 429 or error.status_code >= 500
    return False


def with_retries(what: str, call: Callable[[], T]) -> T:
    """``call()``, retrying transient failures with a growing backoff.

    The last error, or the first one that isn't transient, is raised.
    """
    for attempt in range(1, ATTEMPTS):
        try:
            return call()
        except CivicClerkError as e:
            if not is_transient(e):
                raise
            delay = BACKOFF_SECONDS * attempt
            logger.warning(
                f"Attempt {attempt}/{ATTEMPTS} to read {what} failed ({e}); "
                f"retrying in {delay}s"
            )
            time.sleep(delay)
    return call()


def format_when(starts_at: datetime) -> str:
    """Format a naive local start time, e.g. ``Nov 11, 2026 at 6:00 PM EST``."""
    local = starts_at.replace(tzinfo=EASTERN)
    hour = local.hour % 12 or 12
    return (
        f"{local:%b} {local.day}, {local.year} at {hour}:{local:%M %p} {local.tzname()}"
    )


def format_location(location: EventLocation | None) -> str | None:
    """A meeting's location on one line.

    For example ``Room 220, 125 East Avenue, Norwalk, CT 06851``; ``address1``
    is often free text such as ``Virtual via Zoom``.
    """
    if location is None:
        return None
    region = " ".join(filter(None, [location.state, location.zip_code]))
    parts = [location.address1, location.address2, location.city, region]
    return ", ".join(part.strip() for part in parts if part and part.strip()) or None


def meeting_from_event(event: Event) -> dict:
    return {
        "title": (event.name or "Meeting").strip(),
        "agenda": f"{PORTAL_URL}/event/{event.id}/files",
        "when": format_when(event.starts_at),
        "starts_at": event.starts_at.replace(tzinfo=EASTERN),
        "location": format_location(event.location),
        "zoom": event.virtual_meeting_url,
        "agenda_id": event.agenda_id,
    }


def get_todays_meetings(
    now: datetime | None = None,
    client: CivicClerkClient | None = None,
    read_agendas: bool = False,
) -> list[dict]:
    """Return meetings scheduled for today or tomorrow (US/Eastern).

    Network failures, rate limits and server errors are retried. If the
    meetings still can't be read, the ``CivicClerkError`` is raised so the run
    reports a failure instead of looking like a day with no meetings.

    With ``read_agendas``, each meeting that hasn't started and isn't
    cancelled also gets a ``participation`` entry from its agenda, read with
    the same client (see ``read_participation``).
    """
    now = now or datetime.now(EASTERN)
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    logger.info(f"Reading {TENANT} meetings for {today} and {tomorrow}")

    owns_client = client is None
    client = client or CivicClerkClient(TENANT)
    try:
        events = with_retries(
            "meetings",
            lambda: list(client.iter_events(from_date=today, to_date=tomorrow)),
        )
        meetings = [meeting_from_event(event) for event in events]
        for meeting in meetings:
            logger.info(f"Found meeting: {meeting['title']} on {meeting['when']}")
            if (
                read_agendas
                and meeting["starts_at"] >= now
                and not CANCELLED.search(meeting["title"])
            ):
                meeting["participation"] = read_participation(
                    client, meeting["agenda_id"]
                )
    finally:
        if owns_client:
            client.close()

    logger.info(f"Successfully read {len(meetings)} meetings")
    return meetings


@dataclass
class Participation:
    """Where a meeting's agenda lets the public speak."""

    # Whether there is a public comment / public participation item.
    comment: bool = False
    # What each public hearing is about, in agenda order.
    hearings: list[str] = field(default_factory=list)


# Item names are matched from their start (after any outline number), so
# "Schedule a Public Hearing for ..." or "Discuss rules ... for public
# hearings" don't count. Norwalk agendas use "PUBLIC PARTICIPATION", "PUBLIC
# PARTICIPATION and COMMENT" and "PUBLIC COMMENT" sections, "PUBLIC
# HEARING(S)" sections with an item per hearing, and headings such as "PUBLIC
# HEARING ON THE FYE 2028 OPERATING BUDGET".
OUTLINE_NUMBER = re.compile(r"^(?:[A-Za-z]|\d{1,2}|[ivxIVX]{1,5})[.)]\s+")
PUBLIC_COMMENT = re.compile(
    r"^(?:public|citizens?|residents?)['’]?\s+"
    r"(?:comments?|participation|forum|input|speakers?)\b",
    re.IGNORECASE,
)
PUBLIC_HEARING = re.compile(
    r"^public\s+hearings?\b\s*(?:(?:on|for|regarding|re)\b|[:\-–—])?\s*"
    r"(?:the\s+)?(?P<subject>.*)$",
    re.IGNORECASE,
)
# A hearing heading with nothing under it, e.g. "PUBLIC HEARING" or "- None".
NO_SUBJECT = re.compile(r"^(?:none|n/?a|tbd)?\W*$", re.IGNORECASE)
# Leading application numbers, e.g. "#2026-41 R/M/SP - " or "26-0618-02 – ".
CASE_NUMBER = re.compile(
    r"^#?\d{2,4}-\d{2,4}(?:-\d{1,4})?\s*(?:[A-Z]{1,5}(?:/[A-Z]{1,5})*\s*)?[-–—:]\s*"
)
# Planning and Zoning's trailing "- Public hearing, report & recommended
# action", and the Zoning Board of Appeals' "(continued from <date> meeting)".
HEARING_BOILERPLATE = re.compile(
    r"\s*[-–—]\s*public hearing\b.*$|\s*\(continued from [^)]*\)\s*[-–—]?",
    re.IGNORECASE,
)
ZERO_WIDTH = re.compile("[\u200b‌‍﻿]")


def item_text(name: str | None) -> str:
    """An agenda item's name as one line of plain text.

    Names are HTML and sometimes carry tracked changes; deletions are dropped.
    """
    soup = BeautifulSoup(name or "", "html.parser")
    for deleted in soup.find_all("del"):
        deleted.decompose()
    text = " ".join(ZERO_WIDTH.sub("", " ".join(soup.stripped_strings)).split())
    return OUTLINE_NUMBER.sub("", text)


def hearing_topic(text: str) -> str:
    """What a hearing item is about, without its application number."""
    if match := PUBLIC_HEARING.match(text):
        text = match["subject"]
    text = HEARING_BOILERPLATE.sub("", CASE_NUMBER.sub("", text)).strip(" -–—:")
    return text[:1].upper() + text[1:]


def find_participation(agenda: Agenda) -> Participation:
    """The public comment and public hearing items on an agenda."""
    found = Participation()

    def visit(items: list[AgendaItem]) -> None:
        for item in items:
            text = item_text(item.name)
            if PUBLIC_COMMENT.match(text):
                found.comment = True
            elif hearing := PUBLIC_HEARING.match(text):
                if item.children:
                    # A "PUBLIC HEARINGS" section: each item under it is one.
                    topics = (hearing_topic(item_text(c.name)) for c in item.children)
                    found.hearings.extend(topic for topic in topics if topic)
                elif not NO_SUBJECT.match(hearing["subject"]):
                    found.hearings.append(hearing_topic(text))
                continue
            visit(item.children)

    visit(agenda.items)
    return found


def read_participation(
    client: CivicClerkClient, agenda_id: int | None
) -> Participation | None:
    """Where the public can speak at a meeting, from its agenda.

    Best effort: ``None`` when the meeting has no published agenda or it can't
    be read (after retrying transient failures), so the meeting is still
    posted, just without the note.
    """
    if agenda_id is None:
        logger.info("No agenda to read for public comment")
        return None
    try:
        agenda = with_retries(
            f"agenda {agenda_id}", lambda: client.get_agenda(agenda_id)
        )
    except CivicClerkError as e:
        logger.warning(f"Could not read agenda {agenda_id} ({e}); posting without it")
        return None
    if agenda.raw.get("agendaIsPublish") is False or not agenda.items:
        logger.info(f"Agenda {agenda_id} is not published yet")
        return None
    found = find_participation(agenda)
    logger.info(
        f"Agenda {agenda_id}: public comment: {'yes' if found.comment else 'no'}; "
        f"public hearings: {len(found.hearings)}"
    )
    return found


if __name__ == "__main__":
    for meeting in get_todays_meetings(read_agendas=True):
        print(f"Title: {meeting['title']}")
        print(f"Agenda: {meeting['agenda']}")
        print(f"When: {meeting['when']}")
        print(f"Zoom: {meeting['zoom']}")
        print(f"Participation: {meeting.get('participation')}")
        print()
