import time
from datetime import date, datetime, timedelta

import pytz
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

PORTAL_URL = "https://norwalkct.portal.civicclerk.com/"


def fetch_page_source(url: str = PORTAL_URL) -> str:
    """Render the CivicClerk portal in headless Firefox and return its HTML."""
    firefox_options = Options()
    firefox_options.add_argument("--headless")
    service = Service("/usr/local/bin/geckodriver")
    driver = webdriver.Firefox(service=service, options=firefox_options)
    logger.debug("Firefox WebDriver initialized")
    try:
        driver.get(url)
        logger.info("Loading page...")
        time.sleep(5)  # Wait for JavaScript to load the content
        return driver.page_source
    finally:
        driver.quit()
        logger.debug("WebDriver closed")


# Meeting rows on the portal. The class has changed before (it was
# "cpp-MuiListItem-container" until 2026), so keep the old one as a fallback.
MEETING_SELECTOR = "li.meeting-event, li.cpp-MuiListItem-container"


def parse_meeting_date(text: str) -> date | None:
    """Parse a portal date heading such as "Nov 11,<br>2026" or "NOV 11, 2026"."""
    normalized = " ".join(text.replace(",", ", ").split())
    try:
        return datetime.strptime(normalized, "%b %d, %Y").date()
    except ValueError:
        return None


def parse_meetings(
    page_source: str, now: datetime, url: str = PORTAL_URL
) -> list[dict]:
    """Extract today's and tomorrow's meetings from the rendered portal HTML."""
    soup = BeautifulSoup(page_source, "html.parser")

    wanted_dates = {now.date(), (now + timedelta(days=1)).date()}
    logger.info(f"Looking for meetings on {sorted(str(d) for d in wanted_dates)}")
    meetings = []

    meeting_elements = soup.select(MEETING_SELECTOR)
    if not meeting_elements:
        logger.warning(
            "No meeting entries found on the CivicClerk portal; "
            "the page layout may have changed"
        )
    logger.debug(f"Found {len(meeting_elements)} meeting elements")

    for meeting in meeting_elements:
        try:
            date_heading = meeting.find("h2").get_text(" ", strip=True)
            meeting_date = parse_meeting_date(date_heading)
            if meeting_date is None:
                logger.warning(f"Unrecognized meeting date: {date_heading!r}")
                continue
            if meeting_date not in wanted_dates:
                continue

            title = meeting.find("h3").get_text(strip=True)
            agenda = url.strip("/") + meeting.find("a")["href"]
            meeting_time = meeting.find("p").get_text(strip=True)
            meeting_location_tag = meeting.find("a", title="Go To Event Media")
            meeting_location = (
                meeting_location_tag["href"] if meeting_location_tag else None
            )
            when = f"{meeting_date:%b} {meeting_date.day}, {meeting_date.year}"

            meetings.append(
                {
                    "title": title,
                    "agenda": agenda,
                    "when": f"{when} at {meeting_time}",
                    "where": meeting_location,
                }
            )
            logger.info(f"Found meeting: {title} on {when}")
        except Exception as e:  # noqa: BLE001 - scraping must degrade gracefully
            logger.warning(f"Failed to parse meeting element: {e}")
            continue

    logger.info(f"Successfully read {len(meetings)} meetings")
    return meetings


def get_todays_meetings() -> list[dict]:
    logger.info(f"Reading meetings from: {PORTAL_URL}")
    try:
        page_source = fetch_page_source(PORTAL_URL)
        now = datetime.now(pytz.timezone("US/Eastern"))
        return parse_meetings(page_source, now, PORTAL_URL)
    except Exception as e:  # noqa: BLE001 - scraping must degrade gracefully
        logger.error(f"Failed to read meetings: {e}")
        return []


if __name__ == "__main__":
    meetings = get_todays_meetings()
    for meeting in meetings:
        print(f"Title: {meeting['title']}")
        print(f"Agenda: {meeting['agenda']}")
        print(f"When: {meeting['when']}")
        print(f"Where: {meeting['where']}")
        print()
