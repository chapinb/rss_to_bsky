"""Tests for CivicClerk meetings using a saved API response."""

import json
from datetime import datetime

import httpx
import pytz
from libmuni.civicclerk import CivicClerkClient

from rss_post.norwalk_civic_clerk import format_when, get_todays_meetings
from tests.conftest import FIXTURES

# Response of GET https://norwalkct.api.civicclerk.com/v1/Events for
# 2026-11-11..2026-11-12, captured 2026-11-12.
EVENTS = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
NOV_11_7AM = pytz.timezone("US/Eastern").localize(datetime(2026, 11, 11, 7, 0))


def fake_client(handler) -> CivicClerkClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CivicClerkClient("norwalkct", http_client=http, min_request_interval=0)


def serve_events(requests: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json=EVENTS)

    return fake_client(handler)


def test_requests_today_and_tomorrow():
    requests = []
    get_todays_meetings(NOV_11_7AM, serve_events(requests))

    query = requests[0].url.params["$filter"]
    assert "ge 2026-11-11T00:00:00Z" in query
    assert "le 2026-11-12T23:59:59Z" in query


def test_parses_meetings():
    meetings = get_todays_meetings(NOV_11_7AM, serve_events())

    assert [m["title"] for m in meetings] == [
        "Pension Board of Trustees (CANCELLED)",
        "Ad Hoc Sustainability and Resilience Committee (CANCELLED)",
        "Defined Contribution (CANCELLED)",
        "Recreation, Parks & Cultural Affairs Committee (CANCELLED)",
        "Finance & Claims Committee",
        "Library Board of Directors",
    ]
    assert meetings[0] == {
        "title": "Pension Board of Trustees (CANCELLED)",
        "agenda": "https://norwalkct.portal.civicclerk.com/event/1122/files",
        "when": "Nov 11, 2026 at 6:00 PM EST",
        "where": None,
    }
    finance = next(m for m in meetings if m["title"] == "Finance & Claims Committee")
    assert finance["where"] == "https://norwalkct-org.zoom.us/j/83292194046"


def test_api_failure_yields_no_meetings():
    client = fake_client(lambda request: httpx.Response(503))
    assert get_todays_meetings(NOV_11_7AM, client) == []


def test_network_failure_yields_no_meetings():
    def handler(request):
        raise httpx.ConnectError("unreachable", request=request)

    assert get_todays_meetings(NOV_11_7AM, fake_client(handler)) == []


def test_format_when_uses_daylight_time_in_summer():
    assert format_when(datetime(2026, 7, 4, 9, 5)) == "Jul 4, 2026 at 9:05 AM EDT"
    assert format_when(datetime(2026, 12, 1, 0, 30)) == "Dec 1, 2026 at 12:30 AM EST"
