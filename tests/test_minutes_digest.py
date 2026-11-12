"""Tests for cutting long minutes down to their decisions."""

import re

from rss_post import summarize
from rss_post.minutes_digest import TARGET_CHARS, condense, count_names
from rss_post.norwalk_civic_clerk import plain_text
from tests.conftest import FIXTURES

# Text of real Norwalk minutes, as GET .../v1/Meetings/GetMeetingFileStream(
# fileId=<id>,plainText=true) returned it on 2026-11-12, in full:
# 16321: Planning & Zoning Commission, Oct 21, 2026 (the Manresa Wilds
#   rezoning and master plan approvals, 20 pages);
# 2943: Planning & Zoning Commission, Feb 4, 2026 (a long public hearing,
#   then a motion marked "➢ **");
# 16373: Ad Hoc Sustainability and Resilience Committee, Nov 4, 2026 (no
#   vote records: "** MOTION PASSED ...", then a long appendix).
LONG = {
    int(path.stem): plain_text(path.read_bytes().decode("utf-8"))
    for path in (FIXTURES / "civicclerk_minutes_long").glob("*.txt")
}


def motions(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("**")]


def test_short_minutes_are_unchanged():
    text = "I. CALL TO ORDER\n** MR. A MOVED TO ADJOURN.\n" + "x " * 100
    assert condense(text) == text


def test_long_minutes_are_cut_to_about_the_target():
    for text in LONG.values():
        assert len(text) > TARGET_CHARS * 2
        assert len(condense(text)) <= TARGET_CHARS * 1.2


def test_every_motion_is_kept_with_its_outcome_and_vote():
    # Without condensing, 4 of these 7 motions (both Manresa Wilds approvals
    # among them) are past summarize.MAX_INPUT_CHARS and never sent.
    text = LONG[16321]
    assert [m.start() > summarize.MAX_INPUT_CHARS for m in re.finditer(r"\*\*", text)] == [
        False, False, False, True, True, True, True
    ]  # fmt: skip
    kept = motions(condense(text))
    assert len(kept) == 7
    assert kept[0].startswith("** MS. TABACHNECK MOVED that #2026-69 SP")
    assert kept[0].endswith("be continued to the next Planning & Zoning "
                            "Commission meeting on November 4, 2026. Vote: 8-0.")  # fmt: skip
    westmere = kept[1]
    assert "1 Westmere Avenue" in westmere
    assert westmere.endswith(
        "be APPROVED [conditions omitted] Vote: 8-0, 1 abstaining."
    )
    rezoning, master_plan = kept[3], kept[4]
    assert "rezone 125-acres of Manresa Island" in rezoning
    assert rezoning.endswith("be APPROVED [conditions omitted] Vote: 9-0.")
    assert "Special Permit application" in master_plan
    assert master_plan.endswith("be APPROVED [conditions omitted] Vote: 9-0.")
    # Conditions of approval are left out.
    assert "Public parking capacity be limited" not in condense(text)


def test_headings_name_each_application():
    condensed = condense(LONG[16321]).splitlines()
    assert "III. PUBLIC HEARINGS" in condensed
    assert (
        "C. #2026-41 R/M/SP - Manresa Osprey LLC - Longshore Avenue and 0 and 3 "
        "Manresa Island (District 5, Block 86, Lots 1 and 2) - Zoning regulation "
        "and map amendment to create the Special District - Manresa Wilds (SD-MW) "
        "zoning district and special permit application for a Development Park "
        "Master Plan - Report & recommended action"
    ) in condensed


def test_page_headers_are_dropped():
    condensed = condense(LONG[16321])
    assert "Page 3 of 20" not in condensed
    assert "Planning & Zoning Commission - Minutes" not in condensed


def test_a_motion_can_start_mid_line():
    kept = motions(condense(LONG[2943]))
    assert kept[0].startswith("** MR. KANTOR MOVED")
    assert kept[0].endswith("be APPROVED [conditions omitted] Vote: 8-0.")


def test_a_hearing_keeps_the_start_and_end_of_its_discussion():
    condensed = condense(LONG[2943])
    assert "Atty Liz Suchy began the presentation" in condensed
    assert (
        "WHEREAS, the Commission closed the public hearing on February 4" in condensed
    )


def test_motions_without_a_vote_record_end_at_their_sentence():
    condensed = condense(LONG[16373])
    assert motions(condensed) == [
        (
            "** MR. DELLINGER MADE A MOTION TO ACCEPT THE OCTOBER 14, 2026 REGULAR "
            "MEETING MINUTES."
        ),
        "** MOTION PASSED (MR. LOPEZ ABSTAINED).",
        "** MR. DELLINGER MADE A MOTION TO ADJOURN.",
        "** MOTION PASSED UNANIMOUSLY.",
    ]
    # The discussion after a motion isn't swallowed by it.
    assert "Ms. Trendler presented the Norwalk 2050 Sustainability" in condensed


def test_vote_counts():
    assert count_names("No one") == 0
    assert count_names("") == 0
    assert count_names("Galen Wells and Jacquen Jordan-Byron") == 2
    assert count_names("Chapin Bryce; Tammy Langalis; Doug Hempstead;") == 3
