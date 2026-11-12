"""The weekly look-ahead thread: when it goes out, what it says, and dedup."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from atproto import models
from libmuni.civicclerk import CivicClerkClient

from rss_post import (
    lookahead,
    norwalk_city_calendar,
    norwalk_civic_clerk,
    norwalk_library,
    norwalk_oyster,
)
from rss_post.dedup import (
    EASTERN,
    REPLY_PREFIXES,
    PostRef,
    SeenPosts,
    keys_in_record,
    published_posts,
    reply_key,
)
from rss_post.lookahead import Item, weekly_lookahead
from rss_post.norwalk_feeds import publish
from rss_post.post import Post
from tests.conftest import FIXTURES

SUNDAY_6PM = datetime(2026, 11, 15, 18, 0, tzinfo=EASTERN)
MON, TUE, WED = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)


def meeting(name: str, day: date, hour: int, minute: int = 0) -> Item:
    slug = name.lower().replace(" ", "-")
    return Item(
        name,
        f"https://m.example/{slug}",
        day,
        at(day, hour, minute),
        meeting=True,
    )


def event(name: str, day: date, hour: int | None = None, time_text: str = "") -> Item:
    slug = name.lower().replace(" ", "-")
    starts_at = at(day, hour) if hour is not None else None
    return Item(name, f"https://e.example/{slug}", day, starts_at, time_text)


def loaders(*sources: list[Item]):
    return [
        (f"source {n}", lambda days, items=items: items)
        for n, items in enumerate(sources)
    ]


def text(post: Post) -> str:
    return post.build().build_text()


WEEK_MEETINGS = [
    meeting("Common Council", TUE, 19, 30),
    meeting("Planning Commission", MON, 18),
    meeting("Board of Education: Policy Committee", WED, 18),
]
WEEK_EVENTS = [
    event("Holiday Market", WED, 10),
    event("Fall Festival", MON),
    event("Jazz Night", WED, None, "7:00"),
]


def week_thread(now: datetime = SUNDAY_6PM) -> list[Post]:
    return weekly_lookahead(now, loaders(WEEK_MEETINGS, WEEK_EVENTS))


# When it goes out


@pytest.mark.parametrize(
    "now, due",
    [
        (SUNDAY_6PM - timedelta(minutes=1), False),
        (SUNDAY_6PM, True),
        (SUNDAY_6PM + timedelta(hours=5, minutes=59), True),
        (SUNDAY_6PM + timedelta(days=1), False),  # Monday 6 PM
        (SUNDAY_6PM - timedelta(days=1), False),  # Saturday 6 PM
        (datetime(2026, 11, 15, 23, 30, tzinfo=UTC), True),  # 6:30 PM EST
        (datetime(2026, 11, 15, 20, 30, tzinfo=UTC), False),  # 3:30 PM EST
    ],
)
def test_due_on_sunday_from_6pm_eastern(now, due):
    assert lookahead.is_due(now) is due


def test_covers_monday_to_sunday_after_the_sunday():
    days = lookahead.week_after(SUNDAY_6PM)
    assert days[0] == MON and days[-1] == date(2026, 11, 22)
    assert [d.weekday() for d in days] == list(range(7))


# What it says


def test_root_counts_meetings_and_events_and_links_the_meeting_calendar():
    root = week_thread()[0]

    assert text(root) == (
        "This week in Norwalk: 3 meetings, 3 events\n"
        "Nov 16 – Nov 22, day by day in the replies.\n"
        "City meeting calendar\n#NorwalkCT"
    )
    facets = root.build().build_facets()
    features = [f.features[0] for f in facets]
    assert features[0].uri == norwalk_civic_clerk.PORTAL_URL
    assert features[1].tag == "NorwalkCT"
    assert root.reply_kind is None


def test_one_reply_per_day_in_time_order_continuing_the_chain():
    root, *replies = week_thread()

    assert [text(r) for r in replies] == [
        "Monday, Nov 16\nAll day · Fall Festival\n6 PM · Planning Commission",
        "Tuesday, Nov 17\n7:30 PM · Common Council",
        (
            "Wednesday, Nov 18\n7:00 · Jazz Night\n10 AM · Holiday Market\n"
            "6 PM · Board of Education: Policy Committee"
        ),
    ]
    assert [r.reply_kind for r in replies] == [
        "lookahead-monday",
        "lookahead-tuesday",
        "lookahead-wednesday",
    ]
    assert all(r.reply_root is root for r in replies)
    assert replies[0].reply_parent is None  # i.e. the root
    assert replies[1].reply_parent is replies[0]
    assert replies[2].reply_parent is replies[1]


def test_item_names_link_to_their_pages():
    monday = week_thread()[1]
    links = [f.features[0].uri for f in monday.build().build_facets()]
    assert links == [
        "https://e.example/fall-festival",
        "https://m.example/planning-commission",
    ]


def test_reply_text_starts_with_its_kinds_prefix():
    for reply in week_thread()[1:]:
        assert text(reply).startswith(REPLY_PREFIXES[reply.reply_kind])


def test_nothing_on_means_no_thread():
    assert weekly_lookahead(SUNDAY_6PM, loaders([], [])) == []


def test_items_outside_the_week_are_left_out():
    posts = weekly_lookahead(
        SUNDAY_6PM,
        loaders([meeting("Old", date(2026, 11, 15), 19), meeting("Next", MON, 9)]),
    )
    assert "1 meeting," in text(posts[0])
    assert len(posts) == 2


def test_events_are_capped_per_day_with_a_count_of_the_rest():
    events = [event(f"Event {n}", MON, 9 + n) for n in range(7)]
    root, monday = weekly_lookahead(SUNDAY_6PM, loaders(events))

    assert "0 meetings, 7 events" in text(root)
    assert text(monday).splitlines()[1:] == [
        "9 AM · Event 0",
        "10 AM · Event 1",
        "11 AM · Event 2",
        "12 PM · Event 3",
        "+3 more",
    ]


def test_meetings_are_never_capped():
    meetings = [meeting(f"Board {n}", MON, 9 + n) for n in range(6)]
    posts = weekly_lookahead(SUNDAY_6PM, loaders(meetings))
    assert sum(text(p).count("Board ") for p in posts[1:]) == 6
    assert "+" not in "".join(text(p) for p in posts[1:])


def test_an_event_listed_twice_or_as_a_meeting_counts_once():
    oyster = [event("Meet the Mayor at Odeens", MON, 17)]
    city = [
        event("Meet the Mayor", MON, 17),
        event("Planning Commission", MON, 18),
    ]
    root, monday = weekly_lookahead(
        SUNDAY_6PM, loaders([meeting("Planning Commission", MON, 18)], oyster, city)
    )

    assert "1 meeting, 1 event" in text(root)
    assert text(monday).splitlines()[1:] == [
        "5 PM · Meet the Mayor at Odeens",
        "6 PM · Planning Commission",
    ]


def test_long_names_are_cut():
    name = "A very long event name " * 6
    _, monday = weekly_lookahead(SUNDAY_6PM, loaders([event(name, MON, 9)]))
    line = text(monday).splitlines()[1]
    assert line.endswith("…")
    assert len(line) <= len("9 AM · ") + lookahead.MAX_NAME_LENGTH


def test_busy_day_is_split_into_parts_that_fit():
    meetings = [
        meeting(f"Long-Named Municipal Committee Number {n}", MON, 8, n)
        for n in range(12)
    ]
    _, *replies = weekly_lookahead(SUNDAY_6PM, loaders(meetings))

    assert len(replies) > 1
    assert all(len(text(r)) <= lookahead.REPLY_MAX_LENGTH for r in replies)
    assert text(replies[0]).startswith("Monday, Nov 16\n")
    assert text(replies[1]).startswith("Monday (part 2), Nov 16\n")
    assert [r.reply_kind for r in replies[:2]] == [
        "lookahead-monday",
        "lookahead-monday-2",
    ]
    assert replies[1].reply_parent is replies[0]
    # Every meeting is listed, in order.
    listed = [
        line.split(" · ")[1] for r in replies for line in text(r).splitlines()[1:]
    ]
    assert listed == [m.name for m in meetings]


def test_a_day_too_busy_for_every_part_counts_the_rest():
    meetings = [
        meeting(f"Long-Named Municipal Committee Number {n}", MON, 8, n)
        for n in range(60)
    ]
    _, *replies = weekly_lookahead(SUNDAY_6PM, loaders(meetings))

    assert len(replies) == lookahead.LOOKAHEAD_MAX_PARTS
    assert all(len(text(r)) <= lookahead.REPLY_MAX_LENGTH for r in replies)
    last = text(replies[-1]).splitlines()
    shown = sum(len(text(r).splitlines()) - 1 for r in replies) - 1
    assert last[-1] == f"+{60 - shown} more"


def test_replies_use_up_to_the_bluesky_limit_with_a_margin():
    meetings = [
        meeting(f"Long-Named Municipal Committee Number {n}", MON, 8, n)
        for n in range(12)
    ]
    _, *replies = weekly_lookahead(SUNDAY_6PM, loaders(meetings))
    lengths = [len(text(r)) for r in replies]
    assert max(lengths) > Post.MAX_POST_LENGTH
    assert max(lengths) <= lookahead.REPLY_MAX_LENGTH < 300


@pytest.mark.parametrize(
    "name, tidied",
    [
        (
            "Special Meeting  Recreation ,Parks & Cultural Affairs Nov.18 2026",
            "Special Meeting Recreation, Parks & Cultural Affairs",
        ),
        (
            "Recreation , Parks and Cultural Affairs Public Hearing",
            "Recreation, Parks and Cultural Affairs Public Hearing",
        ),
        ("C.H.O.I.C.E.S. Medicare Counseling", "C.H.O.I.C.E.S. Medicare Counseling"),
        ("Budget Hearing , FY 2027 .", "Budget Hearing, FY 2027."),
    ],
)
def test_names_are_tidied(name, tidied):
    assert lookahead.tidy(name) == tidied


# The real Nov 18 2026 pair: two CivicClerk events (8122 and 8126), each with
# its own agenda, for the same committee 15 minutes apart.
REC_PARKS_HEARING = "Recreation , Parks and Cultural Affairs Public Hearing"
REC_PARKS_SPECIAL = "Special Meeting  Recreation ,Parks & Cultural Affairs Nov.18 2026"


def test_two_meetings_with_similar_names_are_both_listed():
    """Meetings are never merged: each has its own agenda."""
    root, wednesday = weekly_lookahead(
        SUNDAY_6PM,
        loaders(
            [
                meeting(REC_PARKS_HEARING, WED, 18, 45),
                meeting(REC_PARKS_SPECIAL, WED, 19),
            ]
        ),
    )
    assert "2 meetings, 0 events" in text(root)
    assert text(wednesday).splitlines()[1:] == [
        "6:45 PM · Recreation, Parks and Cultural Affairs Public Hearing",
        "7 PM · Special Meeting Recreation, Parks & Cultural Affairs",
    ]


def test_city_calendar_listing_of_a_meeting_is_left_to_the_meeting():
    root, wednesday = weekly_lookahead(
        SUNDAY_6PM,
        loaders(
            [meeting(REC_PARKS_SPECIAL, WED, 19)],
            [replace(event(REC_PARKS_HEARING, WED, 18), starts_at=at(WED, 18, 45))],
        ),
    )
    assert "1 meeting, 0 events" in text(root)
    assert len(text(wednesday).splitlines()) == 2


@pytest.mark.parametrize(
    "event_name, event_start, meeting_name",
    [
        # Similar names, but hours apart.
        (REC_PARKS_HEARING, (16, 0), REC_PARKS_SPECIAL),
        # Same time, different bodies sharing a word or two.
        ("Planning Commission Open House", (19, 0), "Zoning Commission"),
        ("Historical Society Lecture", (19, 0), "Historical Commission"),
        ("Zoning Board of Appeals", (19, 0), "Planning and Zoning Commission"),
        ("Parks Cleanup Day", (19, 0), REC_PARKS_SPECIAL),
    ],
)
def test_distinct_events_and_meetings_stay_separate(
    event_name, event_start, meeting_name
):
    root, _ = weekly_lookahead(
        SUNDAY_6PM,
        loaders(
            [meeting(meeting_name, WED, 19)],
            [replace(event(event_name, WED, 0), starts_at=at(WED, *event_start))],
        ),
    )
    assert "1 meeting, 1 event" in text(root)


# Failing sources


def test_a_failing_source_is_left_out():
    def broken(days):
        raise httpx.ConnectError("down")

    posts = weekly_lookahead(
        SUNDAY_6PM, [("broken", broken)] + loaders(WEEK_MEETINGS, WEEK_EVENTS)
    )
    assert "3 meetings, 3 events" in text(posts[0])


def test_every_source_failing_is_reported():
    def broken(days):
        raise httpx.ConnectError("down")

    with pytest.raises(RuntimeError):
        weekly_lookahead(SUNDAY_6PM, [("a", broken), ("b", broken)])


# Dedup across hourly runs


class Account:
    """A stand-in Bluesky account: posts become records in its history."""

    def __init__(self) -> None:
        self.records: list[tuple[str, models.AppBskyFeedPost.Record]] = []
        self.fail_at: int | None = None
        self.attempts = 0

    def post(self, post: Post) -> PostRef:
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise RuntimeError("rate limited")
        builder = post.build()
        uri = f"at://acct/app.bsky.feed.post/{len(self.records)}"
        self.records.append(
            (
                uri,
                models.AppBskyFeedPost.Record(
                    text=builder.build_text(),
                    facets=builder.build_facets(),
                    created_at="2026-11-15T23:05:00Z",
                    reply=post.reply,
                ),
            )
        )
        return PostRef(uri, "c")

    def history(self) -> SeenPosts:
        feed = [
            SimpleNamespace(post=SimpleNamespace(record=r, uri=u, cid="c"), reason=None)
            for u, r in reversed(self.records)
        ]
        client = MagicMock()
        client.get_author_feed.return_value = SimpleNamespace(feed=feed, cursor=None)
        return SeenPosts(
            published_posts(client, "did:x", since=datetime(2026, 11, 1, tzinfo=UTC))
        )

    def run(self, now: datetime = SUNDAY_6PM) -> tuple[int, int]:
        return publish(self, [("look-ahead", lambda: week_thread(now))], self.history())


def test_hourly_reruns_on_sunday_evening_do_not_repost():
    account = Account()
    assert account.run() == (4, 0)
    for hour in range(19, 24):
        assert account.run(SUNDAY_6PM.replace(hour=hour)) == (0, 0)
    assert len(account.records) == 4


def test_replies_are_read_back_by_thread_and_kind():
    account = Account()
    account.run()
    root_uri = account.records[0][0]
    keys = set().union(*(keys_in_record(r) for _, r in account.records[1:]))
    assert keys == {
        reply_key(root_uri, kind)
        for kind in ("lookahead-monday", "lookahead-tuesday", "lookahead-wednesday")
    }


def test_run_that_fails_halfway_is_resumed_without_duplicates():
    account = Account()
    account.fail_at = 3  # Tuesday's reply.
    assert account.run() == (2, 1)  # Root and Monday; Wednesday has no parent.
    assert [r.text.split("\n")[0] for _, r in account.records] == [
        "This week in Norwalk: 3 meetings, 3 events",
        "Monday, Nov 16",
    ]

    account.fail_at = None
    assert account.run(SUNDAY_6PM.replace(hour=19)) == (2, 0)
    (root, _), (monday, _), (_, tuesday), (_, wednesday) = account.records
    assert tuesday.text.startswith("Tuesday")
    assert tuesday.reply.root.uri == root and tuesday.reply.parent.uri == monday
    assert wednesday.reply.root.uri == root
    assert wednesday.reply.parent.uri == account.records[2][0]
    assert account.run(SUNDAY_6PM.replace(hour=20)) == (0, 0)


def test_next_week_gets_its_own_thread():
    account = Account()
    account.run()
    next_sunday = SUNDAY_6PM + timedelta(days=7)
    # The history keys the root by the day it went out.
    seen = account.history()
    later = meeting("Common Council", next_sunday.date() + timedelta(days=2), 19)
    posts = weekly_lookahead(next_sunday, loaders([later]))
    assert seen.is_new(posts[0])
    # ... but not again on the same Sunday.
    assert not seen.is_new(weekly_lookahead(SUNDAY_6PM, loaders(WEEK_MEETINGS))[0])


# Reading each source for the week


def serve(pages: dict[str, bytes]):
    return patch(
        "rss_post.norwalk_oyster.httpx.get",
        side_effect=lambda url, **_: httpx.Response(
            200, content=pages[url], request=httpx.Request("GET", url)
        ),
    )


def test_civic_clerk_meetings_between_dates():
    requests = []
    events = json.loads((FIXTURES / "civicclerk_events.json").read_text("utf-8"))

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=events)

    client = CivicClerkClient(
        "norwalkct",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_request_interval=0,
    )
    meetings = norwalk_civic_clerk.get_meetings_between(
        date(2026, 11, 9), date(2026, 11, 15), client
    )
    query = requests[0].url.params["$filter"]
    assert "ge 2026-11-09T00:00:00Z" in query and "le 2026-11-15T23:59:59Z" in query
    assert len(meetings) == 6


def test_oyster_events_by_day_from_one_read():
    pages = {
        norwalk_oyster.EVENTS_URL: (
            FIXTURES / "norwalk_oyster_events.json"
        ).read_bytes()
    }
    with serve(pages) as get:
        by_day = norwalk_oyster.get_events_by_day(
            [date(2026, 11, 7), date(2026, 11, 8)]
        )
    assert get.call_count == 1
    assert [len(by_day[d]) for d in sorted(by_day)] == [2, 1]


def test_library_events_by_day_capped_per_day_from_one_read():
    pages = {
        norwalk_library.EVENTS_URL: (
            FIXTURES / "norwalk_library_events.json"
        ).read_bytes()
    }
    days = lookahead.week_after(SUNDAY_6PM)
    with serve(pages) as get:
        by_day = norwalk_library.get_events_by_day(days)
    assert get.call_count == 1
    assert set(by_day) == set(days)
    assert all(
        len(events) <= norwalk_library.MAX_EVENTS_PER_DAY for events in by_day.values()
    )
    assert by_day[WED]


def test_city_calendar_events_by_day_from_one_read():
    url = norwalk_city_calendar.REC_PARKS_URL
    pages = {url: (FIXTURES / "norwalk_rec_parks_calendar.xml").read_bytes()}
    with serve(pages) as get:
        by_day = norwalk_city_calendar.get_events_by_day(url, [date(2026, 11, 14), MON])
    assert get.call_count == 1
    assert by_day[date(2026, 11, 14)]


def test_weekly_lookahead_from_saved_sources():
    """The real loaders, each reading a saved copy of its source."""
    pages = {
        norwalk_oyster.EVENTS_URL: (
            FIXTURES / "norwalk_oyster_events.json"
        ).read_bytes(),
        norwalk_library.EVENTS_URL: (
            FIXTURES / "norwalk_library_events.json"
        ).read_bytes(),
        norwalk_city_calendar.REC_PARKS_URL: (
            FIXTURES / "norwalk_rec_parks_calendar.xml"
        ).read_bytes(),
        norwalk_city_calendar.MAIN_CITY_URL: (
            FIXTURES / "norwalk_main_city_calendar.xml"
        ).read_bytes(),
    }
    boe = (FIXTURES / "norwalk_boe_calendar.ics").read_bytes()
    with (
        serve(pages),
        patch(
            "rss_post.norwalk_boe.httpx.get",
            side_effect=lambda url, **_: httpx.Response(
                200, content=boe, request=httpx.Request("GET", url)
            ),
        ),
        patch(
            "rss_post.lookahead.norwalk_civic_clerk.get_meetings_between",
            side_effect=norwalk_civic_clerk.CivicClerkError("down"),
        ),
    ):
        root, *replies = weekly_lookahead(SUNDAY_6PM)

    assert text(root).startswith("This week in Norwalk: 3 meetings, ")
    assert [text(r).split("\n")[0] for r in replies][:3] == [
        "Monday, Nov 16",
        "Tuesday, Nov 17",
        "Wednesday, Nov 18",
    ]
    assert "5:30 PM · Board of Education: " in text(replies[0])
    assert all(len(text(r)) <= lookahead.REPLY_MAX_LENGTH for r in replies)
