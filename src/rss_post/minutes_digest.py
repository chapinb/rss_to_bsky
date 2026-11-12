"""Long meeting minutes cut down to their decisions, for summarizing.

Planning & Zoning Commission minutes run to 20-45k characters, mostly
presentations, public comment and the full text of each resolution (findings,
"WHEREAS" clauses and numbered conditions of approval). Their decisions are
easy to find, though: Norwalk's clerks mark every motion with ``**``, and P&Z
records each vote as the names that approved, opposed and abstained.

``condense`` keeps what a summary of decisions needs, whole:

- the agenda headings (``III. PUBLIC HEARINGS``, ``A. #2026-41 R/M/SP - ...``),
  which name each application and what it asks for;
- every motion, shortened to its mover and outcome (``... be APPROVED``), with
  its conditions left out and its vote as a tally (``Vote: 8-0, 1 abstaining.``);

and shares what room is left (``TARGET_CHARS`` in all) among the discussion,
keeping the start and end of each passage (who presented what; how a hearing
ended), so meetings with no votes (a hearing continued to another night)
still have something to summarize.

Minutes no longer than ``TARGET_CHARS`` are returned as they are.
"""

import re

# Long minutes are cut to about this many characters. Without the discussion,
# Norwalk's busiest P&Z meetings come to ~3k, so this leaves most of it for
# discussion, and is well under summarize.MAX_INPUT_CHARS.
TARGET_CHARS = 8000
# A motion longer than this is cut to its start (who moved what) and the words
# up to its outcome.
MOTION_HEAD_CHARS = 220
MOTION_TAIL_CHARS = 160
# A heading runs into the discussion after it (the text has no paragraph
# breaks), so it is cut where the discussion seems to start, at most here.
MAX_HEADING_CHARS = 500
# Every passage of discussion keeps at least this much.
MIN_PROSE_CHARS = 200

# A page's running header ends with its page number.
PAGE = re.compile(r"^Page \d+(?: of \d+)?$", re.IGNORECASE)
# A section (I., IV.) or item (A., B.) heading.
HEADING = re.compile(r"^(?:[IVX]+|[A-Z])\.\s")
# Where the discussion after a heading starts: someone doing something.
DISCUSSION_START = re.compile(
    r"(?<=[a-z0-9)])\s+(?=(?:Mr|Ms|Mrs|Dr)\.\s|Atty\s"
    r"|[A-Z][a-z]+ [A-Z][a-z]+,? (?:said|began|explained|presented|noted))"
)
# Where a resolution turns to its conditions, findings or plan lists.
CONDITIONS = re.compile(
    r"\b(?:subject to|to implement|with|for) the following\b"
    r"|\bthe following (?:conditions|modifications|goals)\b",
    re.IGNORECASE,
)
# A motion's outcome, kept when the motion is cut.
OUTCOME = re.compile(
    r"\b(?:APPROVED?|DENIED|DENY|CONTINUED?|TABLED?|WITHDRAWN|RECOMMENDED|"
    r"RECOMMEND|ADOPTED|ADOPT|GRANTED?|AMENDED|PASSED|FAILED)\b"
)
# P&Z's vote record: "<names> approved. <names|No one> opposed. <...> abstained."
VOTE = re.compile(
    r"(?P<yes>[^.]*?)\bapproved\.\s*"
    r"(?P<no>[^.]*?)\s*\bopposed\.\s*"
    r"(?P<abstain>[^.]*?)\s*\babstained\."
)
SECONDED = re.compile(
    r"\s*\b(?:Mr|Ms|Mrs|Dr)\.\s+[\w'’-]+\s+seconded(?: the motion)?\.", re.IGNORECASE
)
# A vote with more names than this isn't a vote record (a board has ~10).
MAX_VOTERS = 25


def clip(text: str, limit: int) -> str:
    """``text`` cut at a word to at most about ``limit`` characters."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def head_and_tail(text: str, limit: int) -> str:
    """The start (two thirds) and end of ``text``, about ``limit`` in all."""
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3].rsplit(" ", 1)[0]
    tail = text[len(text) - (limit - len(head)) :].split(" ", 1)[-1]
    return f"{head} … {tail}"


def count_names(names: str) -> int:
    """How many people a vote record's list names ("No one" is none)."""
    names = names.strip()
    if not names or names.lower().startswith("no one"):
        return 0
    return len([n for n in re.split(r";|,|\band\b", names) if n.strip()])


def page_headers(lines: list[str]) -> set[str]:
    """The running header's lines: those just before 3+ page numbers."""
    seen: dict[str, int] = {}
    for i, line in enumerate(lines):
        if PAGE.match(line):
            for before in lines[max(0, i - 2) : i]:
                seen[before] = seen.get(before, 0) + 1
    return {line for line, n in seen.items() if n >= 3}


def blocks(text: str) -> list[list[str]]:
    """The lines of ``text`` without page headers, grouped into blocks that
    each start at a heading or a motion (``**``, which can start mid-line)."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    headers = page_headers(lines)
    grouped: list[list[str]] = []
    for line in lines:
        if PAGE.match(line) or line in headers:
            continue
        for i, part in enumerate(re.split(r"(?=\*\*)", line)):
            part = part.strip()
            if not part:
                continue
            if i > 0 or HEADING.match(part) or not grouped:
                grouped.append([part])
            else:
                grouped[-1].append(part)
    return grouped


def shorten_motion(motion: str) -> str:
    """A motion without its conditions, cut to its start and outcome."""
    motion = SECONDED.split(motion)[0]
    if conditions := CONDITIONS.search(motion):
        motion = motion[: conditions.start()].rstrip(" ,;:") + " [conditions omitted]"
    if len(motion) > MOTION_HEAD_CHARS + MOTION_TAIL_CHARS:
        outcomes = [m for m in OUTCOME.finditer(motion) if m.start() > MOTION_HEAD_CHARS]
        end = outcomes[-1].end() if outcomes else len(motion)
        start = max(MOTION_HEAD_CHARS, end - MOTION_TAIL_CHARS)
        tail = motion[start : end + 30].split(" ", 1)[-1]
        motion = f"{clip(motion[:MOTION_HEAD_CHARS], MOTION_HEAD_CHARS)} {tail}"
    return motion.strip()


def split_motion(lines: list[str]) -> tuple[str, str]:
    """A motion block as (the motion with its vote tally, what follows it)."""
    text = " ".join(lines)
    vote = VOTE.search(text)
    if vote and count_names(vote["yes"]) <= MAX_VOTERS:
        yes, no, abstain = (count_names(vote[k]) for k in ("yes", "no", "abstain"))
        tally = f"Vote: {yes}-{no}" + (f", {abstain} abstaining" if abstain else "")
        motion = shorten_motion(text[: vote.start()])
        return f"{motion} {tally}.", text[vote.end() :].strip()
    # No vote record (other boards write "** THE MOTION PASSED ..."): the
    # motion ends with its first line that ends a sentence.
    for i, line in enumerate(lines):
        if line.endswith((".", ".)")):
            return shorten_motion(" ".join(lines[: i + 1])), " ".join(lines[i + 1 :])
    return shorten_motion(text), ""


def condense(text: str, target: int = TARGET_CHARS) -> str:
    """``text`` (minutes, as ``plain_text`` leaves them) cut to about
    ``target`` characters, keeping the headings and motions (see the module
    docstring). Returned as is if it's no longer than ``target``."""
    if len(text) <= target:
        return text
    parts: list[tuple[bool, str]] = []  # (kept whole?, text)
    for block in blocks(text):
        if block[0].startswith("**"):
            motion, rest = split_motion(block)
            parts.append((True, motion))
        else:
            joined = " ".join(block)
            rest = joined
            if HEADING.match(joined):
                start = DISCUSSION_START.search(joined)
                if start and start.start() <= MAX_HEADING_CHARS:
                    end = start.start()
                else:
                    end = len(clip(joined, MAX_HEADING_CHARS // 2))
                parts.append((True, joined[:end].strip()))
                rest = joined[end:].strip()
        if rest:
            parts.append((False, rest))
    kept = sum(len(t) + 1 for whole, t in parts if whole)
    discussion = sum(len(t) + 1 for whole, t in parts if not whole)
    share = min(1.0, max(0, target - kept) / discussion) if discussion else 1.0
    return "\n".join(
        t if whole else head_and_tail(t, max(MIN_PROSE_CHARS, int(len(t) * share)))
        for whole, t in parts
    )
