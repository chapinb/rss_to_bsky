"""City of Norwalk meetings from CivicClerk: today's and tomorrow's, and
recent meetings' minutes.

Uses the public CivicClerk API via ``libmuni`` rather than scraping the
JavaScript-rendered portal. Meeting agendas are read to note where the public
can speak (public comment or a public hearing).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    PublishedFile,
)

from rss_post.dedup import CANCELLED
from rss_post.logging_config import get_logger

logger = get_logger(__name__)

TENANT = "norwalkct"
PORTAL_URL = f"https://{TENANT}.portal.civicclerk.com"
EASTERN = ZoneInfo("America/New_York")

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0

# A YouTube video link (youtu.be/ID, youtube.com/watch?v=ID, /live/ID, ...).
YOUTUBE_URL = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|live/|embed/|shorts/))"
    r"([\w-]{11})"
)

T = TypeVar("T")

# Minutes are looked for on meetings this far back. Over Jul-Nov 2026 Norwalk
# published minutes a median of 5 days after the meeting, 90% within 16 days
# and all but one (65 days) within 60. About 7 pages of events (1 request/s).
MINUTES_LOOKBACK = timedelta(days=60)
MINUTES_TYPE = "Minutes"

# An agenda whose items (not counting section headings) total fewer characters
# than this is boilerplate (call to order, minutes, adjournment): nothing to
# summarize.
MIN_AGENDA_ITEM_CHARS = 100
# Longer items (e.g. a list of every account in a budget transfer) are cut.
MAX_AGENDA_ITEM_CHARS = 1000


@dataclass(frozen=True)
class Minutes:
    """A meeting's published minutes."""

    meeting: dict  # As from ``meeting_from_event``.
    file_id: int  # For ``CivicClerkClient.get_file_text``.
    name: str | None
    url: str  # The file in the portal's viewer.
    published_at: datetime  # Eastern.


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


def youtube_id(event: Event) -> str | None:
    """The YouTube id of the meeting's recording, if it has one.

    Norwalk leaves ``youtubeVideoId`` empty and puts a ``youtu.be`` link in
    ``externalMediaUrl`` once the recording is up; before that the field may
    hold the meeting's Zoom link instead, which is not a recording.
    """
    if event.youtube_video_id:
        return event.youtube_video_id
    match = YOUTUBE_URL.search(event.raw.get("externalMediaUrl") or "")
    return match.group(1) if match else None


def meeting_from_event(event: Event) -> dict:
    video = youtube_id(event)
    return {
        "title": (event.name or "Meeting").strip(),
        "agenda": f"{PORTAL_URL}/event/{event.id}/files",
        "when": format_when(event.starts_at),
        "starts_at": event.starts_at.replace(tzinfo=EASTERN),
        "location": format_location(event.location),
        "zoom": event.virtual_meeting_url,
        "agenda_id": event.agenda_id,
        # The recording's YouTube page and thumbnail, once it is posted.
        "recording": video and f"https://www.youtube.com/watch?v={video}",
        "recording_thumb": video and f"https://i.ytimg.com/vi/{video}/hqdefault.jpg",
    }


def read_events(
    from_date: date, to_date: date, client: CivicClerkClient | None = None
) -> list[Event]:
    """Events from ``from_date`` to ``to_date``, in one paged API call.

    Network failures, rate limits and server errors are retried; if the
    events still can't be read, the ``CivicClerkError`` is raised.
    """
    owns_client = client is None
    client = client or CivicClerkClient(TENANT)
    try:
        return with_retries(
            "meetings",
            lambda: list(client.iter_events(from_date=from_date, to_date=to_date)),
        )
    finally:
        if owns_client:
            client.close()


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
    cancelled also gets ``participation`` and ``agenda_outline`` entries from
    its agenda, read once with the same client (see ``read_agenda``,
    ``participation_in`` and ``agenda_outline``).
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
                agenda = read_agenda(client, meeting["agenda_id"])
                meeting["participation"] = participation_in(agenda)
                meeting["agenda_outline"] = agenda_outline(agenda)
    finally:
        if owns_client:
            client.close()

    logger.info(f"Successfully read {len(meetings)} meetings")
    return meetings


def get_recent_meetings(
    days: int, now: datetime | None = None, client: CivicClerkClient | None = None
) -> list[dict]:
    """Meetings from the last ``days`` days (US/Eastern) that have started.

    For follow-ups that appear after a meeting, such as its recording. Raises
    like ``get_todays_meetings``.
    """
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    since = (now - timedelta(days=days)).date()
    logger.info(f"Reading {TENANT} meetings from {since} to {now.date()}")
    meetings = [
        meeting_from_event(event) for event in read_events(since, now.date(), client)
    ]
    meetings = [
        m for m in meetings if since <= m["starts_at"].date() and m["starts_at"] <= now
    ]
    logger.info(f"Read {len(meetings)} meetings since {since}")
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


def read_agenda(client: CivicClerkClient, agenda_id: int | None) -> Agenda | None:
    """A meeting's published agenda.

    Best effort: ``None`` when the meeting has no published agenda or it can't
    be read (after retrying transient failures), so the meeting is still
    posted, just without what its agenda would add.
    """
    if agenda_id is None:
        logger.info("No agenda to read")
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
    return agenda


def participation_in(agenda: Agenda | None) -> Participation | None:
    """Where the public can speak at a meeting, from its agenda (if any)."""
    if agenda is None:
        return None
    found = find_participation(agenda)
    logger.info(
        f"Agenda {agenda.agenda_id}: public comment: "
        f"{'yes' if found.comment else 'no'}; public hearings: {len(found.hearings)}"
    )
    return found


def read_participation(
    client: CivicClerkClient, agenda_id: int | None
) -> Participation | None:
    """Where the public can speak at a meeting, from its agenda.

    Best effort, like ``read_agenda``: ``None`` without a readable published
    agenda.
    """
    return participation_in(read_agenda(client, agenda_id))


def outline_item_text(item: AgendaItem) -> str:
    """An agenda item's name as plain text (``item_text``), capped in length."""
    text = item_text(item.name)
    if len(text) > MAX_AGENDA_ITEM_CHARS:
        text = text[: MAX_AGENDA_ITEM_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return text


def agenda_outline(agenda: Agenda | None) -> str | None:
    """A published agenda as a plain-text outline (for summaries), or None if
    there is none or it is only boilerplate (see ``MIN_AGENDA_ITEM_CHARS``).

    Section headings are lines of their own and items are indented ``-``
    lines, nested as on the agenda.
    """
    if agenda is None or agenda.raw.get("agendaIsPublish") is False:
        return None
    lines = []
    item_chars = 0

    def add(items: list[AgendaItem], depth: int) -> None:
        nonlocal item_chars
        for item in items:
            text = outline_item_text(item)
            if text:
                if item.is_section:
                    lines.append(f"{'  ' * depth}{text}")
                else:
                    lines.append(f"{'  ' * depth}- {text}")
                    item_chars += len(text)
            add(item.children, depth + 1)

    add(agenda.items, 0)
    if item_chars < MIN_AGENDA_ITEM_CHARS:
        return None
    return "\n".join(lines)


def minutes_file(event: Event) -> PublishedFile | None:
    """The event's minutes: the first published after the meeting started.

    Boards often attach the previous meeting's minutes to the next meeting
    ahead of it (for approval); those are published before it starts.
    """
    files = [
        f
        for f in event.published_files
        if f.file_type == MINUTES_TYPE
        and f.published_at is not None
        and f.published_at >= event.starts_at
    ]
    return min(files, key=lambda f: f.published_at, default=None)


def get_recent_minutes(
    now: datetime | None = None, client: CivicClerkClient | None = None
) -> list[Minutes]:
    """Minutes published so far for meetings in the last ``MINUTES_LOOKBACK``."""
    now = now or datetime.now(EASTERN)
    today = now.astimezone(EASTERN).date()
    logger.info(
        f"Reading {TENANT} minutes for meetings since {today - MINUTES_LOOKBACK}"
    )
    found = []
    for event in read_events(today - MINUTES_LOOKBACK, today, client):
        file = minutes_file(event)
        if file is None or file.published_at is None:
            continue
        published_at = file.published_at.replace(tzinfo=EASTERN)
        if published_at > now:
            continue
        meeting = meeting_from_event(event)
        found.append(
            Minutes(
                meeting=meeting,
                file_id=file.file_id,
                name=file.name,
                url=f"{PORTAL_URL}/event/{event.id}/files/agenda/{file.file_id}",
                published_at=published_at,
            )
        )
    logger.info(f"Found minutes for {len(found)} meetings")
    return found


if __name__ == "__main__":
    for meeting in get_todays_meetings(read_agendas=True):
        print(f"Title: {meeting['title']}")
        print(f"Agenda: {meeting['agenda']}")
        print(f"When: {meeting['when']}")
        print(f"Zoom: {meeting['zoom']}")
        print(f"Participation: {meeting.get('participation')}")
        print()
