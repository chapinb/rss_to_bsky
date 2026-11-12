"""Board of Education meetings, using a saved copy of the district's iCal feed."""

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from rss_post import norwalk_boe
from rss_post.dedup import SeenPosts, keys_in_record
from rss_post.norwalk_feeds import NorwalkFeeds
from tests.conftest import FIXTURES

EASTERN = ZoneInfo("America/New_York")
CALENDAR = FIXTURES / "norwalk_boe_calendar.ics"


def calendar() -> str:
    return CALENDAR.read_text()


def response(url: str, status: int = 200, content: bytes = b"") -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("GET", url))


@pytest.fixture
def no_sleep():
    with patch("rss_post.norwalk_boe.time.sleep"):
        yield


@pytest.fixture
def district_site():
    """Serve the saved calendar in place of the district's site."""
    with patch(
        "rss_post.norwalk_boe.httpx.get",
        side_effect=lambda url, **_: response(url, content=CALENDAR.read_bytes()),
    ) as mock_get:
        yield mock_get


def test_meetings_on_a_day_in_time_order():
    meetings = norwalk_boe.meetings_on(calendar(), date(2026, 12, 15))

    assert [m.title for m in meetings] == [
        "SPED Curriculum Subcommittee",
        "Board Business Meeting",
    ]
    sped, business = meetings
    assert sped.starts_at == datetime(2026, 12, 15, 17, 30, tzinfo=EASTERN)
    assert sped.when == "Dec 15, 2026 at 5:30 PM EST"
    # Each meeting has a link of its own on the district's calendar.
    assert sped.url == (
        "https://www.norwalkps.org/board-of-education1/boe-calendar/~occur_id/37583752"
    )
    assert business.url.endswith("/~occur_id/37583646")


def test_online_meeting_has_zoom_link_and_no_location():
    (meeting,) = norwalk_boe.meetings_on(calendar(), date(2026, 11, 18))

    assert meeting.title == "Policy Committee"
    assert meeting.location == ""
    assert meeting.zoom == "https://zoom.us/j/99613801200"


def test_in_person_meeting_keeps_location_and_finds_zoom_in_description():
    (meeting,) = norwalk_boe.meetings_on(calendar(), date(2026, 11, 17))

    assert meeting.location == "125 East Ave, Norwalk, CT 06851, USA, Chambers"
    assert meeting.zoom == "https://zoom.us/j/92933087405"


def test_meeting_without_zoom_link():
    meetings = norwalk_boe.meetings_on(calendar(), date(2027, 1, 19))

    assert meetings[-1].title == "Board Business Meeting"
    assert meetings[-1].zoom == ""


def test_no_meetings_on_a_quiet_day():
    assert norwalk_boe.meetings_on(calendar(), date(2026, 11, 12)) == []


def test_title_case_keeps_acronyms_and_mixed_case_titles():
    assert (
        norwalk_boe.title_case("AD-HOC INCLUSION & EDUCATIONAL JUSTICE COMMITTEE")
        == "Ad-Hoc Inclusion & Educational Justice Committee"
    )
    assert norwalk_boe.title_case("MLL CURRICULUM SUBCOMMITTEE") == (
        "MLL Curriculum Subcommittee"
    )
    assert norwalk_boe.title_case("Special Meeting") == "Special Meeting"


def test_folded_lines_escapes_and_time_zones():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1@www.norwalkps.org\r\n"
        "DTSTART:20261201T230000Z\r\n"
        "SUMMARY:Special Meeting\\, Budget\r\n"
        "LOCATION:City Hall\\; Room\r\n  101\r\n"
        "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:2@www.norwalkps.org\r\n"
        "DTSTART;TZID=America/Chicago:20261201T170000\r\n"
        "SUMMARY:Retreat\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:3@www.norwalkps.org\r\n"
        "DTSTART;VALUE=DATE:20261201\r\nSUMMARY:All day\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    meetings = norwalk_boe.meetings_on(ics, date(2026, 12, 1))

    assert [(m.title, m.starts_at.hour) for m in meetings] == [
        ("Special Meeting, Budget", 18),
        ("Retreat", 18),
    ]
    assert meetings[0].location == "City Hall; Room 101"


def test_reads_the_district_calendar(district_site):
    meetings = norwalk_boe.get_meetings_on(date(2026, 11, 17))

    assert [m.title for m in meetings] == ["Board Business Meeting"]
    assert district_site.call_args.args[0] == norwalk_boe.CALENDAR_ICS_URL


def test_server_errors_are_retried_then_raised(no_sleep):
    with (
        patch(
            "rss_post.norwalk_boe.httpx.get",
            side_effect=lambda url, **_: response(url, status=503),
        ) as mock_get,
        pytest.raises(httpx.HTTPStatusError),
    ):
        norwalk_boe.get_meetings_on(date(2026, 11, 17))
    assert mock_get.call_count == norwalk_boe.ATTEMPTS


def test_client_errors_are_not_retried(no_sleep):
    with (
        patch(
            "rss_post.norwalk_boe.httpx.get",
            side_effect=lambda url, **_: response(url, status=404),
        ) as mock_get,
        pytest.raises(httpx.HTTPStatusError),
    ):
        norwalk_boe.get_meetings_on(date(2026, 11, 17))
    assert mock_get.call_count == 1


def boe_posts(now: datetime):
    return NorwalkFeeds(posting_frequency=None).get_todays_boe_meetings(now)


def card_titles(posts) -> list[str]:
    return [p.embed.external.title.removeprefix("Board of Education: ") for p in posts]


class Account:
    """The bot's Bluesky account: what it posted, as the history read at startup."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    def run(self, now: datetime) -> list:
        """One run of the bot: post what's new, as ``publish`` would."""
        seen = SeenPosts(self.keys)
        posted = [p for p in boe_posts(now) if seen.is_new(p)]
        for post in posted:
            seen.add(post)

            class Record:
                embed = post.embed
                facets = post.build().build_facets()
                text = post.build().build_text()

            self.keys |= keys_in_record(Record(), now)
        return posted


def test_reads_today_and_tomorrow_from_one_fetch(district_site):
    posts = boe_posts(datetime(2026, 11, 17, 7, 5, tzinfo=EASTERN))

    assert card_titles(posts) == ["Board Business Meeting", "Policy Committee"]
    assert district_site.call_count == 1


def test_meeting_post_has_time_card_agenda_and_zoom_links(district_site):
    post = boe_posts(datetime(2026, 11, 17, 7, 5, tzinfo=EASTERN))[0]

    text = post.build().build_text()
    assert text == (
        "Board of Education: Board Business Meeting\n"
        "Nov 17, 2026 at 7:00 PM EST\n"
        "Agendas on BoardDocs\n"
        "Join on Zoom"
    )
    facets = [f.features[0].uri for f in post.build().build_facets()]
    assert facets == [norwalk_boe.AGENDAS_URL, "https://zoom.us/j/92933087405"]
    card = post.embed.external
    assert card.uri.endswith("/~occur_id/37583303")
    assert card.title == "Board of Education: Board Business Meeting"
    assert card.description == (
        "Nov 17, 2026 at 7:00 PM EST · 125 East Ave, Norwalk, CT 06851, USA, Chambers"
    )
    assert post.link == card.uri
    assert post.day == date(2026, 11, 17)


def test_meetings_that_have_started_are_not_posted(district_site):
    posts = boe_posts(datetime(2026, 12, 15, 18, 0, tzinfo=EASTERN))

    assert card_titles(posts) == [
        "Board Business Meeting",
        "MLL Curriculum Subcommittee",  # Tomorrow's.
    ]


def test_hourly_reruns_on_the_same_day_do_not_repost(district_site):
    account = Account()

    first = account.run(datetime(2026, 12, 15, 7, 0, tzinfo=EASTERN))
    assert card_titles(first) == [
        "SPED Curriculum Subcommittee",
        "Board Business Meeting",
        "MLL Curriculum Subcommittee",
    ]
    for hour in range(8, 24):
        assert account.run(datetime(2026, 12, 15, hour, 0, tzinfo=EASTERN)) == []


def test_meetings_go_out_the_day_before_and_again_on_the_day(district_site):
    account = Account()

    day_before = account.run(datetime(2026, 11, 16, 7, 0, tzinfo=EASTERN))
    later_that_day = account.run(datetime(2026, 11, 16, 12, 0, tzinfo=EASTERN))
    on_the_day = account.run(datetime(2026, 11, 17, 7, 0, tzinfo=EASTERN))

    assert card_titles(day_before) == [
        "Curriculum & Instruction Committee",  # Today's.
        "Board Business Meeting",  # Tomorrow's.
    ]
    assert later_that_day == []
    assert card_titles(on_the_day) == ["Board Business Meeting", "Policy Committee"]
    assert on_the_day[0].link == day_before[1].link


def test_missed_7am_run_is_caught_up_once(district_site):
    account = Account()

    assert len(account.run(datetime(2026, 11, 17, 9, 0, tzinfo=EASTERN))) == 2
    assert account.run(datetime(2026, 11, 17, 10, 0, tzinfo=EASTERN)) == []
