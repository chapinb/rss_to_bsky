"""Tests for posting meeting minutes, using saved CivicClerk data."""

import json
import re
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from atproto import models
from pypdf import PdfWriter

from rss_post import minutes_digest, norwalk_civic_clerk, summarize
from rss_post.dedup import PostRef, SeenPosts, keys_in_record, reply_key
from rss_post.norwalk_civic_clerk import get_recent_minutes
from rss_post.norwalk_feeds import (
    CITY_SEAL,
    MINUTES_SUMMARY_MAX_CHARS,
    NorwalkFeeds,
    publish,
)
from rss_post.post import Post
from tests.conftest import FIXTURES
from tests.test_norwalk_civic_clerk import fake_client

# Response of GET https://norwalkct.api.civicclerk.com/v1/Events for
# 2026-10-27..2026-11-05 (its two pages joined), captured 2026-11-12.
EVENTS = json.loads((FIXTURES / "civicclerk_minutes_events.json").read_text("utf-8"))[
    "value"
]
EASTERN = ZoneInfo("America/New_York")
PORTAL = "https://norwalkct.portal.civicclerk.com"


# Text of real Norwalk minutes, as GET .../v1/Meetings/GetMeetingFileStream(
# fileId=<id>,plainText=true) returned it on 2026-11-12, cut after about 5000
# characters. The API returns an empty 200 for the other files in EVENTS.
MINUTES_TEXTS = {
    int(path.stem): path.read_bytes().decode("utf-8")
    for path in (FIXTURES / "civicclerk_minutes").glob("*.txt")
}
FILE_STREAM = re.compile(r"GetMeetingFileStream\(fileId=(\d+),plainText=true\)")
PDF_STREAM = re.compile(r"GetMeetingFileStream\(fileId=(\d+),plainText=false\)")
# Norwalk Ordinance Committee minutes (file 16478), as downloaded 2026-11-27:
# it has a text layer, but the API's text for it was still empty then.
ORDINANCE_PDF = (FIXTURES / "civicclerk_minutes_pdf" / "16478.pdf").read_bytes()


def serve(
    requests: list | None = None,
    texts: dict[int, str] | None = None,
    file_requests: list | None = None,
    files_up: bool = True,
    pdfs: dict[int, bytes] | None = None,
    pdf_requests: list | None = None,
):
    """A client serving the saved events within each request's date range,
    the saved minutes texts (or ``texts``), and the files in ``pdfs`` (an
    empty body, the API's "unavailable", for others)."""
    texts = MINUTES_TEXTS if texts is None else texts
    pdfs = pdfs or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if match := PDF_STREAM.search(request.url.path):
            file_id = int(match[1])
            if pdf_requests is not None:
                pdf_requests.append(file_id)
            return httpx.Response(
                200,
                content=pdfs.get(file_id, b""),
                headers={"Content-Type": "application/pdf"},
            )
        if match := FILE_STREAM.search(request.url.path):
            file_id = int(match[1])
            if file_requests is not None:
                file_requests.append(file_id)
            if not files_up:
                return httpx.Response(503)
            return httpx.Response(200, text=texts.get(file_id, ""))
        if request.url.path.startswith("/v1/Meetings/"):
            # Meeting posts read agendas (for public comment); none saved here.
            return httpx.Response(404)
        if requests is not None:
            requests.append(request)
        query = request.url.params["$filter"]
        start, end = re.findall(r"(\d{4}-\d\d-\d\d)T", query)
        return httpx.Response(
            200,
            json={"value": [e for e in EVENTS if start <= e["eventDate"][:10] <= end]},
        )

    return fake_client(handler)


def test_minutes_cover_meetings_in_the_lookback():
    requests = []
    get_recent_minutes(datetime(2026, 11, 12, 9, tzinfo=EASTERN), serve(requests))

    query = requests[0].url.params["$filter"]
    assert "ge 2026-09-13T00:00:00Z" in query
    assert "le 2026-11-12T23:59:59Z" in query


def test_recent_minutes_link_the_file_in_the_portal():
    found = get_recent_minutes(datetime(2026, 11, 6, 12, tzinfo=EASTERN), serve())

    council = next(m for m in found if m.meeting["title"] == "City Council")
    assert council.file_id == 16343
    assert council.name == "City Council Minutes 10.27.26"
    assert council.url == f"{PORTAL}/event/1060/files/agenda/16343"
    assert council.published_at == datetime(2026, 10, 30, 12, 29, 54, 47000, EASTERN)
    assert council.meeting["agenda"] == f"{PORTAL}/event/1060/files"


def test_minutes_published_before_the_meeting_are_the_previous_meetings():
    # Fair Rent's Nov 4 meeting carries its Oct 7 minutes, published Nov 3.
    found = get_recent_minutes(datetime(2026, 11, 6, 12, tzinfo=EASTERN), serve())
    titles = [m.meeting["title"] for m in found]

    assert "Fair Rent Commission" not in titles
    # Not yet published at the time of the run (Nov 12).
    assert not any(t.startswith("Public Safety") for t in titles)


class Runs:
    """Hourly runs of meetings and minutes; what each posts is read back the
    way the next run reads the account's history."""

    def __init__(self):
        self.history: dict[str, PostRef] = {}
        self.sent: list[tuple[Post, PostRef]] = []
        # Minutes texts served (default: the saved ones), whether file text
        # requests succeed, and the file ids requested.
        self.texts: dict[int, str] | None = None
        self.files_up = True
        self.file_requests: list[int] = []
        # Minutes files (PDFs) served, and the file ids downloaded.
        self.pdfs: dict[int, bytes] = {}
        self.pdf_requests: list[int] = []
        # Stands in for the LLM (summarize.summarize) in runs with summaries,
        # unless ``llm`` is set: then the real client runs with LLM_API_KEY set
        # against ``llm``, an httpx handler for the provider's API.
        self.summarizer = MagicMock(return_value="- Item one\n- Item two")
        self.llm = None
        # (posts sent, failures) of the last run's ``publish``.
        self.result: tuple[int, int] | None = None

    def run(
        self, now: datetime, meetings: bool = False, summaries: bool = False
    ) -> list[Post]:
        def send(post):
            ref = PostRef(f"at://p/{len(self.sent) + 1}", "c")
            self.sent.append((post, ref))
            return ref

        client = MagicMock()
        client.post.side_effect = send
        already_sent = len(self.sent)
        feeds = NorwalkFeeds(timedelta(hours=1))
        seen = SeenPosts(self.history)
        sources = [("minutes", lambda: feeds.get_meeting_minutes(now))]
        if meetings:
            sources.insert(
                0, ("meetings", lambda: feeds.get_todays_norwalk_meetings(now))
            )
        if summaries:
            sources.append(("summaries", lambda: feeds.get_minutes_summaries(seen)))
        if self.llm is None:
            llm = [
                patch.object(summarize, "configured", return_value=True),
                patch.object(summarize, "enabled", return_value=True),
                patch.object(summarize, "summarize", self.summarizer),
            ]
        else:
            transport = httpx.MockTransport(self.llm)
            llm = [
                patch.dict("os.environ", {"LLM_API_KEY": "test-key"}),
                patch.object(
                    summarize,
                    "_new_client",
                    side_effect=lambda: httpx.Client(transport=transport),
                ),
            ]
        summarize.reset()  # Each run is a new process.
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    norwalk_civic_clerk,
                    "CivicClerkClient",
                    side_effect=lambda _: serve(
                        texts=self.texts,
                        file_requests=self.file_requests,
                        files_up=self.files_up,
                        pdfs=self.pdfs,
                        pdf_requests=self.pdf_requests,
                    ),
                )
            )
            stack.enter_context(patch.object(norwalk_civic_clerk.time, "sleep"))
            for patcher in llm:
                stack.enter_context(patcher)
            self.result = publish(client, sources, seen)
        sent = self.sent[already_sent:]
        for post, ref in sent:
            builder = post.build()
            record = models.AppBskyFeedPost.Record(
                text=builder.build_text(),
                facets=builder.build_facets(),
                embed=post.embed,
                created_at=now.astimezone(UTC).isoformat(),
                reply=post.reply,
            )
            # As ``published_posts`` reads it back.
            read_back = PostRef(ref.uri, ref.cid, is_reply=post.reply is not None)
            for key in keys_in_record(record, now):
                self.history[key] = read_back
        return [post for post, _ in sent]

    def meeting_post(self, title: str, day: date) -> PostRef:
        return next(
            ref
            for post, ref in self.sent
            if post.reply is None
            and post.embed.external.title == title
            and post.day == day
        )


def text(post: Post) -> str:
    return post.build().build_text()


def test_minutes_reply_to_the_meeting_post_from_the_day_of_the_meeting():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    runs.run(datetime(2026, 10, 28, 7, tzinfo=EASTERN), meetings=True)

    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN))

    replies = {text(p): p for p in posts if p.reply is not None}
    council = replies["Minutes posted: City Council, Oct 27, 2026"]
    root = runs.meeting_post("City Council", date(2026, 10, 27))
    assert council.reply.root.uri == council.reply.parent.uri == root.uri
    assert council.reply_kind == "minutes"
    assert council.embed.external.uri == f"{PORTAL}/event/1060/files/agenda/16343"
    assert council.embed.external.title == "City Council Minutes 10.27.26"
    assert council.thumb_url == CITY_SEAL
    # Posted the day before and on the day: the on-the-day post is the root.
    harbor = replies["Minutes posted: Harbor Management Commission, Oct 28, 2026"]
    on_the_day = runs.meeting_post("Harbor Management Commission", date(2026, 10, 28))
    assert harbor.reply.root.uri == on_the_day.uri

    # Later runs skip what's been posted, and pick up newly published minutes.
    assert runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN)) == []
    [forestry] = runs.run(datetime(2026, 10, 30, 15, tzinfo=EASTERN))
    assert text(forestry) == "Minutes posted: Urban Forestry Commission, Oct 27, 2026"
    assert forestry.reply is not None


def test_minutes_reply_to_the_day_before_post_if_that_is_all_there_is():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)

    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN))

    [harbor] = [p for p in posts if "Harbor" in text(p)]
    the_day_before = runs.meeting_post(
        "Harbor Management Commission", date(2026, 10, 27)
    )
    assert harbor.reply.root.uri == the_day_before.uri


def test_minutes_without_a_meeting_post_are_posted_on_their_own_once():
    runs = Runs()
    [sustainability, shellfish] = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN))

    assert shellfish.reply is None
    assert text(shellfish) == "Minutes posted: Shellfish Commission, Nov 5, 2026"
    assert shellfish.embed.external.uri == f"{PORTAL}/event/996/files/agenda/16369"
    assert shellfish.embed.external.description == (
        "Shellfish Commission · Nov 5, 2026 at 6:30 PM EST"
    )
    assert text(sustainability).startswith(
        "Minutes posted: Ad Hoc Sustainability and Resilience Committee"
    )
    assert runs.run(datetime(2026, 11, 6, 13, tzinfo=EASTERN)) == []


def test_minutes_replied_to_are_not_posted_again_once_the_meeting_post_is_gone():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    assert runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN))

    # The meeting posts leave the 30-day history; the replies are newer.
    runs.history = {
        key: ref for key, ref in runs.history.items() if "/files on " not in key
    }
    assert runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN)) == []


def test_only_recently_published_minutes_are_posted():
    # A first run doesn't post the backlog: of the 15 sets of minutes
    # published by Nov 12, only those from the last three days.
    runs = Runs()
    [safety] = runs.run(datetime(2026, 11, 12, 9, tzinfo=EASTERN))
    assert text(safety).startswith(
        "Minutes posted: Public Safety and General Government Committee"
    )
    assert len(runs.run(datetime(2026, 11, 2, 12, tzinfo=EASTERN))) == 5


def test_reply_with_a_card_is_also_keyed_by_its_link():
    ref = models.ComAtprotoRepoStrongRef.Main(uri="at://p/root", cid="c")
    post = Post().with_headline("Minutes posted: Board, Oct 1, 2026")
    post.with_embed_card("https://a.example/minutes", "Minutes")
    record = models.AppBskyFeedPost.Record(
        text=text(post),
        embed=post.embed,
        created_at="2026-10-02T12:00:00Z",
        reply=models.AppBskyFeedPost.ReplyRef(root=ref, parent=ref),
    )

    assert keys_in_record(record) == {
        "at://p/root minutes",
        "https://a.example/minutes",
    }


def alternatives() -> tuple[Post, Post, Post]:
    """Minutes replying to the day's post, else the day before's, else alone."""
    standalone = Post().with_embed_card("https://x.example/minutes", "t")
    on_eve = Post().replying_to("https://x.example/m on 1", "minutes")
    on_the_day = Post().replying_to("https://x.example/m on 2", "minutes")
    on_the_day.otherwise(on_eve.otherwise(standalone))
    return on_the_day, on_eve, standalone


def test_reply_falls_back_through_its_alternatives():
    root = PostRef("at://p/root", "c")
    client = MagicMock()
    on_the_day, on_eve, _ = alternatives()
    seen = SeenPosts({"https://x.example/m on 1": root})

    assert publish(client, [("a", lambda: [on_the_day])], seen) == (1, 0)
    assert client.post.call_args.args[0] is on_eve
    assert on_eve.reply.root.uri == root.uri

    client = MagicMock()
    on_the_day, _, standalone = alternatives()
    assert publish(client, [("a", lambda: [on_the_day])], SeenPosts()) == (1, 0)
    assert client.post.call_args.args[0] is standalone


SUMMARY = "AI summary of the minutes:\n• Item one\n• Item two"


def summaries(posts: list[Post]) -> list[Post]:
    return [p for p in posts if p.reply_kind == "minutes-summary"]


def sent_ref(runs: Runs, post: Post) -> PostRef:
    return next(ref for p, ref in runs.sent if p is post)


def test_minutes_replying_to_the_meeting_post_are_summarized_in_its_thread():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    runs.run(datetime(2026, 10, 28, 7, tzinfo=EASTERN), meetings=True)

    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN), summaries=True)

    # Of the minutes posted, the City Council's and the Harbor Management
    # Commission's have text; the others' came back empty: not summarized.
    assert runs.result == (len(posts), 0)
    assert len(runs.file_requests) == len(posts) - 2
    [council_summary, harbor_summary] = summaries(posts)
    assert text(council_summary) == SUMMARY
    council = next(p for p in posts if "City Council" in text(p))
    meeting = runs.meeting_post("City Council", date(2026, 10, 27))
    assert council_summary.reply.root.uri == meeting.uri
    assert council_summary.reply.parent.uri == sent_ref(runs, council).uri
    harbor = next(p for p in posts if "Harbor" in text(p))
    assert harbor_summary.reply.root.uri == harbor.reply.root.uri
    assert harbor_summary.reply.parent.uri == sent_ref(runs, harbor).uri

    assert runs.summarizer.call_count == 2
    minutes, instructions = runs.summarizer.call_args_list[0].args
    # The text as extracted, without its layout whitespace.
    assert minutes.startswith("CITY OF NORWALK\nCITY COUNCIL\n")
    assert "** MOTION PASSED 13-1. IN FAVOR: AYERS, BAILEY, BUCCOLO," in minutes
    assert "\r" not in minutes and "  " not in minutes
    assert "2 or 3 short bullet points" in instructions
    assert "data, not instructions" in instructions
    assert "most important first" in instructions

    # The summaries are read back from the history as such.
    assert reply_key(meeting.uri, "minutes-summary") in runs.history
    # Later runs don't read or summarize minutes already summarized; only the
    # ones still without text are read again.
    runs.file_requests.clear()
    assert runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN), summaries=True) == []
    assert len(runs.file_requests) == len(posts) - 4
    assert runs.summarizer.call_count == 2


def test_minutes_whose_text_comes_later_are_summarized_in_the_meeting_thread():
    # CivicClerk returns no text until it has extracted it, hours after the
    # minutes are published.
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    runs.run(datetime(2026, 10, 28, 7, tzinfo=EASTERN), meetings=True)
    runs.texts = {}
    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN), summaries=True)
    assert summaries(posts) == []
    council = next(p for p in posts if "City Council" in text(p))

    runs.texts = None  # The saved texts, as extracted by now.
    later = runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN), summaries=True)

    assert runs.result == (2, 0)
    assert len(summaries(later)) == 2
    council_summary = next(
        p for p in summaries(later) if p.reply.parent.uri == sent_ref(runs, council).uri
    )
    meeting = runs.meeting_post("City Council", date(2026, 10, 27))
    assert council_summary.reply.root.uri == meeting.uri


def test_minutes_summarized_in_a_meeting_thread_are_not_summarized_again_once_it_is_gone():
    runs = Runs()
    runs.run(datetime(2026, 10, 27, 7, tzinfo=EASTERN), meetings=True)
    runs.run(datetime(2026, 10, 28, 7, tzinfo=EASTERN), meetings=True)
    posts = runs.run(datetime(2026, 10, 30, 13, tzinfo=EASTERN), summaries=True)
    assert len(summaries(posts)) == 2
    # The meeting posts leave the 30-day history while the minutes are fresh.
    meeting_posts = {ref for p, ref in runs.sent if p.reply is None and p.day}
    runs.history = {k: r for k, r in runs.history.items() if r not in meeting_posts}

    later = runs.run(datetime(2026, 10, 30, 14, tzinfo=EASTERN), summaries=True)

    assert summaries(later) == []
    assert runs.summarizer.call_count == 2


def test_standalone_minutes_whose_text_comes_later_are_summarized():
    runs = Runs()
    runs.texts = {}
    posted = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(posted) == 2
    runs.summarizer.assert_not_called()

    runs.texts = None
    later = runs.run(datetime(2026, 11, 6, 13, tzinfo=EASTERN), summaries=True)

    assert [r.reply.root.uri for r in later] == [
        sent_ref(runs, posted[0]).uri,
        sent_ref(runs, posted[1]).uri,
    ]
    assert [text(r) for r in later] == [SUMMARY, SUMMARY]
    # Once summarized, they aren't read or summarized again.
    runs.file_requests.clear()
    assert runs.run(datetime(2026, 11, 6, 14, tzinfo=EASTERN), summaries=True) == []
    assert runs.file_requests == []
    assert runs.summarizer.call_count == 2


def test_minutes_are_retried_only_while_fresh():
    runs = Runs()
    runs.texts = {}
    runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    runs.file_requests.clear()
    runs.texts = None
    # Four days on: past MINUTES_FRESHNESS (other minutes are fresh by then).
    runs.run(datetime(2026, 11, 10, 12, tzinfo=EASTERN), summaries=True)
    assert not {16369, 16373} & set(runs.file_requests)


def test_standalone_minutes_are_summarized_in_their_own_thread():
    runs = Runs()
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)

    [sustainability, shellfish, *replies] = posts
    assert [r.reply.root.uri for r in replies] == [
        sent_ref(runs, sustainability).uri,
        sent_ref(runs, shellfish).uri,
    ]
    assert all(r.reply.parent.uri == r.reply.root.uri for r in replies)
    assert [text(r) for r in replies] == [SUMMARY, SUMMARY]
    assert sorted(runs.file_requests) == [16369, 16373]
    assert runs.run(datetime(2026, 11, 6, 13, tzinfo=EASTERN), summaries=True) == []
    assert runs.summarizer.call_count == 2


def test_minutes_without_text_are_not_summarized(caplog):
    # The API extracts no text from scanned PDFs (and some others).
    runs = Runs()
    runs.texts = {16369: "", 16373: "CITY OF NORWALK\r\n  MINUTES  \r\n"}
    with caplog.at_level("INFO"):
        posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(posts) == 2
    assert runs.result == (2, 0)
    runs.summarizer.assert_not_called()
    assert "Not summarizing the minutes of Shellfish Commission: 0 chars" in (
        caplog.text
    )


def test_unreadable_minutes_skip_the_summary_not_the_minutes():
    runs = Runs()
    runs.files_up = False
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(posts) == 2
    assert runs.result == (2, 0)
    runs.summarizer.assert_not_called()


def blank_pdf() -> bytes:
    """A valid PDF with no text layer, like a scan."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_minutes_without_api_text_are_read_from_the_pdf(caplog):
    runs = Runs()
    runs.texts = {16373: MINUTES_TEXTS[16373]}  # None from the API for 16369.
    runs.pdfs = {16369: ORDINANCE_PDF}
    with caplog.at_level("INFO"):
        posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)

    assert len(summaries(posts)) == 2
    assert runs.pdf_requests == [16369]  # Only where the API had no text.
    from_pdf = runs.summarizer.call_args_list[1].args[0]
    assert from_pdf.startswith("Norwalk Ordinance Committee\n")
    assert "  " not in from_pdf
    assert "Minutes 16369: 2809 chars of text (from the PDF)" in caplog.text


@pytest.mark.parametrize("pdf", [b"%PDF-1.7 not really", blank_pdf()])
def test_minutes_without_text_in_the_pdf_either_are_not_summarized(pdf):
    runs = Runs()
    runs.texts = {16373: MINUTES_TEXTS[16373]}
    runs.pdfs = {16369: pdf}
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(summaries(posts)) == 1
    assert runs.result == (len(posts), 0)


def test_oversized_minutes_pdf_is_not_read(caplog):
    runs = Runs()
    runs.texts = {16373: MINUTES_TEXTS[16373]}
    runs.pdfs = {16369: ORDINANCE_PDF}
    with (
        patch.object(norwalk_civic_clerk, "MAX_PDF_BYTES", len(ORDINANCE_PDF) - 1),
        caplog.at_level("INFO"),
    ):
        posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(summaries(posts)) == 1
    assert "over the" in caplog.text


def test_failed_minutes_summary_is_retried_by_the_next_run():
    runs = Runs()
    runs.summarizer.return_value = None
    assert len(runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)) == 2
    runs.summarizer.return_value = "• Item one"
    later = runs.run(datetime(2026, 11, 6, 13, tzinfo=EASTERN), summaries=True)
    assert len(summaries(later)) == 2
    assert runs.summarizer.call_count == 4


def test_without_an_llm_key_minutes_are_only_logged(caplog):
    feeds = NorwalkFeeds(timedelta(hours=1))
    seen = SeenPosts()
    now = datetime(2026, 11, 6, 12, tzinfo=EASTERN)
    with (
        patch.object(summarize, "configured", return_value=False),
        patch.object(summarize, "summarize") as llm,
        patch.object(
            norwalk_civic_clerk, "CivicClerkClient", side_effect=lambda _: serve()
        ),
        caplog.at_level("INFO"),
    ):
        result = publish(
            MagicMock(),
            [
                ("minutes", lambda: feeds.get_meeting_minutes(now)),
                ("summaries", lambda: feeds.get_minutes_summaries(seen)),
            ],
            seen,
        )
    assert result == (2, 0)
    llm.assert_not_called()
    assert "LLM_API_KEY not set; would summarize the" in caplog.text
    assert "minutes of Shellfish Commission: CITY OF NORWALK | NORWALK" in caplog.text


def llm_answer(content="• Item one\n• Item two"):
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    }


class LLM:
    """An OpenAI-compatible API answering with ``responses`` in turn (an
    ``httpx.Response``, or an exception to raise), repeating the last one."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


def test_real_llm_client_summarizes_the_minutes():
    runs = Runs()
    runs.llm = LLM(httpx.Response(200, json=llm_answer()))
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert [text(p) for p in summaries(posts)] == [SUMMARY, SUMMARY]
    assert runs.result == (4, 0)
    assert len(runs.llm.requests) == 2
    body = json.loads(runs.llm.requests[0].content)
    assert body["messages"][0]["content"].startswith(
        "You summarize City of Norwalk, Connecticut public meeting minutes"
    )
    assert "SUSTAINABILITY" in body["messages"][1]["content"]


def test_long_minutes_are_cut_to_their_decisions_before_asking():
    # Full Planning & Zoning minutes (35,937 chars as plain text), served as
    # if they were the Shellfish Commission's.
    pz = (FIXTURES / "civicclerk_minutes_long" / "16321.txt").read_bytes()
    runs = Runs()
    runs.llm = LLM(httpx.Response(200, json=llm_answer()))
    runs.texts = {16369: pz.decode("utf-8")}
    runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    [request] = runs.llm.requests
    sent = json.loads(request.content)["messages"][1]["content"]
    opening, closing = summarize.untrusted_tags(summarize.untrusted_nonce())
    assert sent.startswith(f"{opening}\n")
    assert sent.endswith(f"\n{closing}")
    assert len(sent) < minutes_digest.TARGET_CHARS * 1.2
    # The Manresa Wilds approvals, past the first 20,000 chars of the minutes.
    assert sent.count("be APPROVED [conditions omitted] Vote: 9-0.") == 3


def test_bad_llm_key_skips_minutes_summaries_without_failing_the_run():
    runs = Runs()
    runs.llm = LLM(httpx.Response(401, json={"error": {"message": "bad key"}}))
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    # The minutes went out, nothing failed, and after the rejected request
    # the LLM wasn't asked again (and the other minutes weren't read).
    assert len(posts) == 2 and not summaries(posts)
    assert runs.result == (2, 0)
    assert len(runs.llm.requests) == 1
    assert len(runs.file_requests) == 1


def test_llm_timeout_skips_minutes_summaries_without_failing_the_run():
    runs = Runs()
    runs.llm = LLM(httpx.ReadTimeout("timed out"))
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(posts) == 2 and not summaries(posts)
    assert runs.result == (2, 0)
    assert len(runs.llm.requests) == 1


def test_minutes_summary_reply_always_fits_bluesky():
    assert len("AI summary of the minutes:\n") + MINUTES_SUMMARY_MAX_CHARS <= 300


def test_overlong_minutes_summary_keeps_the_leading_bullets_that_fit():
    first = "• Approved a 3-year land records system contract, $62,400 a year"
    second = "• Approved two 1-year extensions of that contract"
    third = "• Recommended a 5-year police equipment contract, " + "detail " * 30
    runs = Runs()
    runs.llm = LLM(
        httpx.Response(200, json=llm_answer(f"{first}\n{second}\n{third}")),
        httpx.Response(200, json=llm_answer("- " + "word " * 80)),
    )
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert runs.result == (4, 0)
    two_bullets, one_bullet = (text(p) for p in summaries(posts))
    assert two_bullets == f"AI summary of the minutes:\n{first}\n{second}"
    # A lone bullet too long to fit is shortened at a word.
    assert one_bullet.startswith("AI summary of the minutes:\n• word word")
    assert one_bullet.endswith("…")
    assert len(one_bullet) <= 300


def test_unexpected_error_in_minutes_summaries_is_not_a_run_failure():
    runs = Runs()
    runs.summarizer.side_effect = RuntimeError("bug")
    posts = runs.run(datetime(2026, 11, 6, 12, tzinfo=EASTERN), summaries=True)
    assert len(posts) == 2
    assert runs.result == (2, 0)


def test_minutes_text_drops_layout_whitespace():
    raw = MINUTES_TEXTS[16343]
    assert raw.startswith("                            CITY OF NORWALK \r\n")
    plain = norwalk_civic_clerk.plain_text(raw)
    assert plain.startswith(
        "CITY OF NORWALK\nCITY COUNCIL\nREGULAR MEETING MINUTES - OCTOBER 27, 2026\n"
    )
    assert "\r" not in plain and "  " not in plain and "\n\n" not in plain
    assert len(plain) < len(raw) * 0.85
