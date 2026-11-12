"""Today's and tomorrow's City of Norwalk meetings from CivicClerk.

Uses the public CivicClerk API via ``libmuni`` rather than scraping the
JavaScript-rendered portal.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from libmuni.civicclerk import CivicClerkClient, CivicClerkError, Event

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

TENANT = "norwalkct"
PORTAL_URL = f"https://{TENANT}.portal.civicclerk.com"
EASTERN = ZoneInfo("America/New_York")


def format_when(starts_at: datetime) -> str:
    """Format a naive local start time, e.g. ``Nov 11, 2026 at 6:00 PM EST``."""
    local = starts_at.replace(tzinfo=EASTERN)
    hour = local.hour % 12 or 12
    return (
        f"{local:%b} {local.day}, {local.year} at {hour}:{local:%M %p} {local.tzname()}"
    )


def meeting_from_event(event: Event) -> dict:
    return {
        "title": (event.name or "Meeting").strip(),
        "agenda": f"{PORTAL_URL}/event/{event.id}/files",
        "when": format_when(event.starts_at),
        "where": event.virtual_meeting_url,
    }


def get_todays_meetings(
    now: datetime | None = None, client: CivicClerkClient | None = None
) -> list[dict]:
    """Return meetings scheduled for today or tomorrow (US/Eastern)."""
    now = now or datetime.now(EASTERN)
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    logger.info(f"Reading {TENANT} meetings for {today} and {tomorrow}")

    owns_client = client is None
    client = client or CivicClerkClient(TENANT)
    try:
        events = list(client.iter_events(from_date=today, to_date=tomorrow))
    except CivicClerkError as e:
        logger.error(f"Failed to read meetings: {e}")
        return []
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
        print(f"Where: {meeting['where']}")
        print()
