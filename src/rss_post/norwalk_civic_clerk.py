"""Today's and tomorrow's City of Norwalk meetings from CivicClerk.

Uses the public CivicClerk API via ``libmuni`` rather than scraping the
JavaScript-rendered portal.
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from libmuni.civicclerk import (
    APIStatusError,
    CivicClerkClient,
    CivicClerkError,
    Event,
    EventLocation,
    NetworkError,
)

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

TENANT = "norwalkct"
PORTAL_URL = f"https://{TENANT}.portal.civicclerk.com"
EASTERN = ZoneInfo("America/New_York")

ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def is_transient(error: CivicClerkError) -> bool:
    """Whether retrying might help: network failures, rate limits and 5xx."""
    if isinstance(error, NetworkError):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code == 429 or error.status_code >= 500
    return False


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
    }


def get_todays_meetings(
    now: datetime | None = None, client: CivicClerkClient | None = None
) -> list[dict]:
    """Return meetings scheduled for today or tomorrow (US/Eastern).

    Network failures, rate limits and server errors are retried. If the
    meetings still can't be read, the ``CivicClerkError`` is raised so the run
    reports a failure instead of looking like a day with no meetings.
    """
    now = now or datetime.now(EASTERN)
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    logger.info(f"Reading {TENANT} meetings for {today} and {tomorrow}")

    owns_client = client is None
    client = client or CivicClerkClient(TENANT)
    try:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                events = list(client.iter_events(from_date=today, to_date=tomorrow))
                break
            except CivicClerkError as e:
                if attempt == ATTEMPTS or not is_transient(e):
                    raise
                delay = BACKOFF_SECONDS * attempt
                logger.warning(
                    f"Attempt {attempt}/{ATTEMPTS} to read meetings failed ({e}); "
                    f"retrying in {delay}s"
                )
                time.sleep(delay)
    finally:
        if owns_client:
            client.close()

    meetings = [meeting_from_event(event) for event in events]
    for meeting in meetings:
        logger.info(f"Found meeting: {meeting['title']} on {meeting['when']}")
    logger.info(f"Successfully read {len(meetings)} meetings")
    return meetings


if __name__ == "__main__":
    for meeting in get_todays_meetings():
        print(f"Title: {meeting['title']}")
        print(f"Agenda: {meeting['agenda']}")
        print(f"When: {meeting['when']}")
        print(f"Zoom: {meeting['zoom']}")
        print()
