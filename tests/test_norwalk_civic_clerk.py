"""Tests for CivicClerk meetings using a saved API response."""

import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from libmuni.civicclerk import Agenda, APIStatusError, CivicClerkClient, NetworkError

from rss_post.norwalk_civic_clerk import (
    MAX_AGENDA_ITEM_CHARS,
    Participation,
    agenda_outline,
    find_participation,
    format_when,
    get_todays_meetings,
    read_participation,
)
from tests.conftest import FIXTURES

# Response of GET https://norwalkct.api.civicclerk.com/v1/Events for
# 2026-11-11..2026-11-12, captured 2026-11-12.
EVENTS = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))
EASTERN = ZoneInfo("America/New_York")
NOV_11_7AM = datetime(2026, 11, 11, 7, 0, tzinfo=EASTERN)


def saved_agenda(agenda_id: int | str) -> dict:
    """A saved response of GET https://norwalkct.api.civicclerk.com/v1/Meetings/{id}.

    Real Norwalk agendas captured 2026-11-12, trimmed to the agenda's id,
    publish flags and each item's id, number, name, order and children.
    """
    path = FIXTURES / "civicclerk_agendas" / f"{agenda_id}.json"
    return json.loads(path.read_text("utf-8"))


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
        "starts_at": datetime(2026, 11, 11, 18, 0, tzinfo=EASTERN),
        "location": "Hybrid via Zoom and Room 220",
        "zoom": None,
        "agenda_id": None,
        "recording": None,
        "recording_thumb": None,
    }
    defined = next(m for m in meetings if m["title"].startswith("Defined"))
    assert defined["location"] == (
        "Hybrid via Zoom and Room 220, 125 East Avenue, 2nd Floor, Norwalk, CT 06851"
    )
    sustainability = next(m for m in meetings if "Sustainability" in m["title"])
    assert sustainability["location"] is None
    finance = next(m for m in meetings if m["title"] == "Finance & Claims Committee")
    assert finance["location"] == "Virtual via Zoom"
    assert finance["zoom"] == "https://norwalkct-org.zoom.us/j/83292194046"


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


def participation_in(agenda_id: int) -> Participation:
    return find_participation(Agenda.from_api(saved_agenda(agenda_id)))


@pytest.mark.parametrize(
    "agenda_id",
    [
        8805,  # Finance & Claims Committee: PUBLIC PARTICIPATION
        8841,  # Library Board of Directors
        # Ordinance Committee: "PUBLIC PARTICIPATION and COMMENT", and a
        # PUBLIC HEARING section with nothing under it.
        8813,
        8801,  # Conservation Commission: PUBLIC COMMENT
        8748,  # Recreation & Parks: a "Schedule a Public Hearing for ..." item
        8817,  # Board of Ethics: a "Discuss rules ... for public hearings" item
    ],
)
def test_public_comment_without_hearings(agenda_id):
    assert participation_in(agenda_id) == Participation(comment=True)


@pytest.mark.parametrize(
    "agenda_id",
    [
        8624,  # Board of Estimate & Taxation
        8840,  # Blight Hearings: citation hearings, not public hearings
    ],
)
def test_no_public_comment_or_hearings(agenda_id):
    assert participation_in(agenda_id) == Participation()


def test_public_hearing_section_and_comment():
    # Land Use & Building Management Committee: a PUBLIC HEARING section with
    # an item per hearing, then PUBLIC PARTICIPATION.
    assert participation_in(8735) == Participation(
        comment=True,
        hearings=[
            (
                "Naming of the Police Headquarters Plaza in honor of Retired Police "
                "Chief and Former Mayor Harry W. Rilling."
            ),
            (
                "Proposed disposition of City property abutting the rear of 18/20 "
                "Cleveland Terrace Dr. along Martin Luther King Jr. Drive"
            ),
        ],
    )


def test_hearing_item_named_public_hearing():
    # "Public hearing on the naming of ..." under a PUBLIC HEARING section.
    assert participation_in(8791).hearings == [
        (
            "Naming of the Norwalk High School Band storage building at Andrews field "
            "in honor of Douglas Oravez."
        )
    ]


def test_hearing_headings_with_their_subject():
    # Budget hearing: "PUBLIC HEARING ON THE FYE 2028 OPERATING BUDGET".
    assert participation_in(8781) == Participation(
        hearings=["FYE 2028 OPERATING BUDGET", "FYE 2028 CAPITAL BUDGET"]
    )


def test_planning_and_zoning_hearings_drop_case_numbers_and_boilerplate():
    found = participation_in(8738)
    assert [h[:60] for h in found.hearings] == [
        "Manresa Osprey LLC - Longshore Avenue and 0 and 3 Manresa Is",
        "Wall Street Neighborhood Association – Zoning regulation tex",
    ]
    assert not any("Public hearing" in h for h in found.hearings)
    assert found.comment is False


def test_tracked_deletions_are_dropped():
    # One hearing's name carries <del>/<ins> tracked changes.
    hearings = participation_in(8758).hearings
    assert len(hearings) == 3
    assert hearings[2].startswith("City of Norwalk – Zoning Regulation Text")
    assert "<" not in "".join(hearings)


def test_zoning_board_of_appeals_hearings():
    hearings = participation_in(8759).hearings
    assert [h.split(" – ")[0] for h in hearings] == [
        "Leslie Dale Dunn, Trustee",
        "Ten Willard Apartments LLC",
        "Michael & Emily Giunta",
    ]


def serve_agenda(response: httpx.Response, requests: list | None = None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return response

    return fake_client(handler)


def test_read_participation():
    requests = []
    client = serve_agenda(httpx.Response(200, json=saved_agenda(8805)), requests)

    assert read_participation(client, 8805) == Participation(comment=True)
    assert requests[0].url.path == "/v1/Meetings/8805"


def test_no_agenda_is_not_read():
    requests = []
    client = serve_agenda(httpx.Response(500), requests)

    assert read_participation(client, None) is None
    assert requests == []


def test_unpublished_agenda_has_no_participation():
    # Recreation, Parks and Cultural Affairs Public Hearing, a week out.
    client = serve_agenda(httpx.Response(200, json=saved_agenda(8835)))
    assert read_participation(client, 8835) is None


def test_agenda_failure_is_retried_then_ignored(no_sleep):
    requests = []
    client = serve_agenda(httpx.Response(503), requests)

    assert read_participation(client, 8805) is None
    assert len(requests) == 3
    assert [c.args[0] for c in no_sleep.call_args_list] == [2.0, 4.0]


def test_missing_agenda_is_ignored_without_retrying():
    requests = []
    client = serve_agenda(httpx.Response(404), requests)

    assert read_participation(client, 1) is None
    assert len(requests) == 1


def test_unreadable_agenda_is_ignored():
    client = serve_agenda(httpx.Response(200, text="<html>"))
    assert read_participation(client, 8805) is None


def test_meetings_with_agendas():
    def handler(request):
        if request.url.path.startswith("/v1/Meetings/"):
            agenda_id = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json=saved_agenda(agenda_id))
        return httpx.Response(200, json=EVENTS)

    meetings = get_todays_meetings(NOV_11_7AM, fake_client(handler), True)

    assert {m["title"]: m.get("participation", "not read") for m in meetings} == {
        # Cancelled meetings' agendas aren't read.
        "Pension Board of Trustees (CANCELLED)": "not read",
        "Ad Hoc Sustainability and Resilience Committee (CANCELLED)": "not read",
        "Defined Contribution (CANCELLED)": "not read",
        "Recreation, Parks & Cultural Affairs Committee (CANCELLED)": "not read",
        "Finance & Claims Committee": Participation(comment=True),
        "Library Board of Directors": Participation(comment=True),
    }
    # The same read gives the agenda's outline, for summaries.
    finance = next(m for m in meetings if m["title"] == "Finance & Claims Committee")
    assert finance["agenda_outline"] == outline("8805")
    assert "agenda_outline" not in meetings[0]
    # Without read_agendas, none are read.
    assert all(
        "participation" not in m
        for m in get_todays_meetings(NOV_11_7AM, fake_client(handler))
    )


def outline(agenda: int | str | dict) -> str | None:
    raw = agenda if isinstance(agenda, dict) else saved_agenda(agenda)
    return agenda_outline(Agenda.from_api(raw))


def test_agenda_outline_nests_sections_and_items_as_plain_text():
    assert outline(8841).splitlines()[2:13] == [
        "ACCEPTANCE OF MINUTES",
        "  - Regular Meeting: 11.12.26",
        "  - Revised Meeting Minutes 10.8.26",
        "PUBLIC PARTICIPATION",
        "REPORTS",
        "  - President",
        "    - Introduction of New Board Members Ray Cooke Amy Kule Elsa P. Obuchowski",
        "  - Library Director",
        (
            "    - Request to close the library Saturday, December 26, 2026 "
            "(NOTE: Library is closed December 25 and 27)"
        ),
        (
            "    - Request to approve the updated Code of Conduct policy "
            "Attachment 1: Current Policy Attachment 2: Requested revision "
            "(approved date area in red awaiting approval)"
        ),
        "    - Heath Department Partnership: Know Your Numbers",
    ]


def test_agenda_outline_caps_long_items():
    # The ARPA transfer item lists every account: thousands of characters.
    lines = outline(8805).splitlines()
    arpa = next(line for line in lines if "ARPA transfer" in line)
    assert arpa.startswith("  - Approve ARPA transfer requests")
    assert arpa.endswith("…")
    assert max(len(line) for line in lines) <= len("  - ") + MAX_AGENDA_ITEM_CHARS
    assert "<" not in "".join(lines)


def test_agenda_outline_of_a_short_agenda():
    assert "  - 30 Friendly Road: Unkempt Construction site" in outline(8840)


def test_unpublished_agenda_has_no_outline():
    assert outline(8835) is None
    assert agenda_outline(None) is None


def test_boilerplate_agenda_has_no_outline():
    def item(id, name, section=False, children=()):
        return {
            "id": id,
            "agendaObjectItemName": name,
            "isSection": section,
            "sortOrder": id,
            "childItems": list(children),
        }

    agenda = {
        "id": 1,
        "agendaIsPublish": True,
        "items": [
            item(1, "CALL TO ORDER", True),
            item(2, "ROLL CALL", True),
            item(3, "ACCEPTANCE OF MINUTES", True, [item(4, "October 8, 2026")]),
            item(5, "PUBLIC PARTICIPATION", True),
            item(6, "ADJOURNMENT", True),
        ],
    }
    assert outline(agenda) is None
