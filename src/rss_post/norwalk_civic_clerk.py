from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time

from rss_post.logging_config import get_logger

logger = get_logger(__name__)


def get_todays_meetings():
    url = "https://norwalkct.portal.civicclerk.com/"
    logger.info(f"Reading meetings from: {url}")

    try:
        # Set up Selenium WebDriver for Firefox
        firefox_options = Options()
        firefox_options.add_argument("--headless")  # Run in headless mode
        service = Service(
            "/usr/local/bin/geckodriver"
        )  # Update with the path to your geckodriver
        driver = webdriver.Firefox(service=service, options=firefox_options)
        logger.debug("Firefox WebDriver initialized")

        driver.get(url)
        logger.info("Loading page...")
        time.sleep(5)  # Wait for JavaScript to load the content

        soup = BeautifulSoup(driver.page_source, "html.parser")
        driver.quit()
        logger.debug("Page loaded and WebDriver closed")

        now = datetime.now()
        today = now.strftime("%b %d, %Y")
        tomorrow = (now + timedelta(days=1)).strftime("%b %d, %Y")
        logger.info(f"Looking for meetings on {today} or {tomorrow}")
        meetings = []

        meeting_elements = soup.find_all("li", class_="cpp-MuiListItem-container")
        logger.debug(f"Found {len(meeting_elements)} meeting elements")

        for meeting in meeting_elements:
            try:
                date = meeting.find("h2").text.strip().replace("\n", " ")
                if today in date or tomorrow in date:
                    title = meeting.find("h3").text.strip()
                    agenda = url.strip("/") + meeting.find("a")["href"]
                    meeting_time = meeting.find("p").text.strip()
                    meeting_location_tag = meeting.find("a", title="Go To Event Media")
                    meeting_location = (
                        meeting_location_tag["href"] if meeting_location_tag else None
                    )

                    meeting_info = {
                        "title": title,
                        "agenda": agenda,
                        "when": date + " at " + meeting_time,
                        "where": meeting_location,
                    }
                    meetings.append(meeting_info)
                    logger.info(f"Found meeting: {title} on {date}")
            except Exception as e:
                logger.warning(f"Failed to parse meeting element: {e}")
                continue

        logger.info(f"Successfully read {len(meetings)} meetings")
        return meetings

    except Exception as e:
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
