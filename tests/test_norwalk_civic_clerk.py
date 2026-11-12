"""Tests for CivicClerk meetings using a saved API response."""

import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from libmuni.civicclerk import APIStatusError, CivicClerkClient, NetworkError

from rss_post.norwalk_civic_clerk import format_when, get_todays_meetings
from tests.conftest import FIXTURES

# Response of GET https://norwalkct.api.civicclerk.com/v1/Events for
# 2026-11-11..2026-11-12, captured 2026-11-12.
EVENTS = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
NOV_11_7AM = datetime(2026, 11, 11, 7, 0, tzinfo=ZoneInfo("America/New_York"))


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


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("rss_post.norwalk_civic_clerk.time.sleep") as sleep:
        yield sleep


def test_api_failure_raises_after_retries():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    with pytest.raises(APIStatusError):
        get_todays_meetings(NOV_11_7AM, fake_client(handler))
    assert len(requests) == 3


def test_network_failure_raises_after_retries(no_sleep):
    def handler(request):
        raise httpx.ConnectError("unreachable", request=request)

    with pytest.raises(NetworkError):
        get_todays_meetings(NOV_11_7AM, fake_client(handler))
    assert [c.args[0] for c in no_sleep.call_args_list] == [2.0, 4.0]


def test_transient_failure_is_retried():
    responses = [httpx.Response(502), httpx.Response(200, json=EVENTS)]

    meetings = get_todays_meetings(
        NOV_11_7AM, fake_client(lambda request: responses.pop(0))
    )
    assert meetings == get_todays_meetings(NOV_11_7AM, serve_events())


def test_client_error_is_not_retried():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400)

    with pytest.raises(APIStatusError):
        get_todays_meetings(NOV_11_7AM, fake_client(handler))
    assert len(requests) == 1


def test_format_when_uses_daylight_time_in_summer():
    assert format_when(datetime(2026, 7, 4, 9, 5)) == "Jul 4, 2026 at 9:05 AM EDT"
    assert format_when(datetime(2026, 12, 1, 0, 30)) == "Dec 1, 2026 at 12:30 AM EST"
