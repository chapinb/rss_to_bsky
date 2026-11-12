"""Tests for CivicClerk meeting parsing against a saved copy of the portal."""

from datetime import datetime
from unittest.mock import patch

import pytest
from selenium.common.exceptions import WebDriverException

from rss_post import norwalk_civic_clerk
from rss_post.norwalk_civic_clerk import parse_meetings
from tests.conftest import FIXTURES

# Rendered https://norwalkct.portal.civicclerk.com/ captured 2026-11-12,
# with <script>/<style> removed.
PORTAL_HTML = (FIXTURES / "civicclerk_portal.html").read_text(encoding="utf-8")
NOV_11_7AM = datetime(2026, 11, 11, 7, 0)


def test_parses_todays_and_tomorrows_meetings():
    meetings = parse_meetings(PORTAL_HTML, NOV_11_7AM)

    titles = [m["title"] for m in meetings]
    assert titles == [
        "Pension Board of Trustees (CANCELLED)",
        "Ad Hoc Sustainability and Resilience Committee (CANCELLED)",
        "Defined Contribution (CANCELLED)",
        "Recreation, Parks & Cultural Affairs Committee (CANCELLED)",
        "Finance & Claims Committee",
        "Library Board of Directors",
    ]
    assert "City Council" not in titles  # Nov 10, yesterday


def test_parses_meeting_details():
    meetings = parse_meetings(PORTAL_HTML, NOV_11_7AM)

    assert meetings[0] == {
        "title": "Pension Board of Trustees (CANCELLED)",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/1122/files",
        "when": "Nov 11, 2026 at 6:00 PM EST",
        "where": None,
    }
    finance = next(m for m in meetings if m["title"] == "Finance & Claims Committee")
    assert finance["where"] == "https://norwalkct-org.zoom.us/j/83292194046"


def test_unrelated_page_yields_no_meetings():
    assert parse_meetings("<html><body></body></html>", NOV_11_7AM) == []


def test_browser_failure_yields_no_meetings():
    with patch.object(
        norwalk_civic_clerk,
        "fetch_page_source",
        side_effect=WebDriverException("geckodriver missing"),
    ):
        assert norwalk_civic_clerk.get_todays_meetings() == []


def test_no_meeting_entries_logs_a_warning(caplog):
    parse_meetings("<html><body></body></html>", NOV_11_7AM)
    assert "No meeting entries found" in caplog.text


@pytest.mark.parametrize(
    "heading",
    ["Nov 11, 2026", "NOV 11, 2026", "Nov 11,<br/>2026", "Nov 11,\n  2026"],
)
def test_date_heading_variants(heading):
    html = f"""
        <ul><li class="meeting-event">
          <a href="/event/1/files"></a>
          <h2>{heading}</h2><p>6:00 PM EST</p><h3>Test Board</h3>
        </li></ul>
    """
    meetings = parse_meetings(html, NOV_11_7AM)
    assert [m["when"] for m in meetings] == ["Nov 11, 2026 at 6:00 PM EST"]


def test_unparseable_date_is_skipped():
    html = """
        <ul><li class="meeting-event">
          <a href="/event/1/files"></a><h2>TBD</h2><p>-</p><h3>Test Board</h3>
        </li></ul>
    """
    assert parse_meetings(html, NOV_11_7AM) == []
