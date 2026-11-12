# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses `uv` as the package manager and build tool.

### Development Setup
- `uv sync` - Install dependencies and set up development environment
- `uv run pytest` - Run tests
- `uv run pytest tests/test_specific.py` - Run a single test file
- `uv run pytest-watcher` - Run tests continuously during development
- `uvx prek run --all-files` - Run lint/format hooks (prek, a drop-in pre-commit replacement using `.pre-commit-config.yaml`)
- `uvx prek install` - Install the git hook so they run on commit

### Running the Application
- `uv run norwalk <hours>` - Run the main application (hours = posting frequency)
- `uv run norwalk 24 --dry-run` - Run in dry-run mode (prints to console instead of posting)
- `uv run norwalk 24 --dry-run --log-level DEBUG` - Run with detailed debug logging
- `uv run norwalk 24 --log-level INFO` - Run with informational logging (default)
- `uv run norwalk 24 --log-level WARNING` - Run with only warnings and errors

### Build and Package
- `uv build` - Build the package
- `uv lock` - Update the lockfile

## Architecture

This is a Python application that monitors RSS feeds for Norwalk, CT municipal content and posts updates to Bluesky social media. 

### Core Components

**Main Entry Point**: `src/rss_post/norwalk_feeds.py:main()` - CLI entry point that orchestrates feed processing

**Feed Processing**: `NorwalkFeeds` class handles multiple RSS sources:
- City of Norwalk calendar events and news flashes  
- Norwalk Current stories (formerly Nancy on Norwalk)
- CT Mirror stories (filtered for Norwalk mentions)
- City meeting schedules via the CivicClerk API (libmuni)
- Board of Education meetings from the Norwalk Public Schools calendar's iCal feed
- Norwalk Oyster newsletter issues and today's top-rated community events
- Norwalk Public Library events (today's notable community and adult programs)
- Today's community events from the city's Recreation & Parks and Main City calendars
- A Sunday-evening look-ahead thread of the coming week's meetings and events

**Post Building**: `src/rss_post/post.py:Post` - Builder pattern for constructing social media posts with length constraints (250 chars), HTML stripping, and link formatting. A post can be a threaded follow-up: `replying_to(root, kind, parent=None)` names the post(s) it replies to by dedup key (from the account's history) or by a `Post` sent earlier in the run; `publish` resolves them to root/parent refs (`reply_to(root_ref, parent_ref)`) and skips the reply if they aren't found

**Output Adapters**: `src/rss_post/bsky.py`
- `Bluesky` class - Posts to actual Bluesky API using atproto; `post()` returns the created post's `PostRef` (uri/cid) so a thread can be built in one run. Replies are sent with `send_post(..., reply_to=...)`
- `Stdout` class - Dry-run mode that prints to console (replies indented under their parent) and returns stand-in refs

**RSS Processing**: `src/rss_post/read_rss.py` - Thin wrapper around feedparser library

**Card thumbnails**: `src/rss_post/media.py` - Finds a card image from the feed item (media:content, media:thumbnail, image enclosure, first `<img>`), falling back to the linked page's `og:image`; `Bluesky` downloads it (≤1 MB) and uploads it as the card's thumb. Best effort: failures post the card without a thumbnail

**Meetings**: `src/rss_post/norwalk_civic_clerk.py` - Today's meetings from the CivicClerk API via `libmuni`. For meetings still to be posted (not started, not cancelled), the structured agenda (`get_agenda`) is read with the same client and `find_participation` matches item names (from their start, so "Schedule a Public Hearing for ..." doesn't count) for public comment / public participation and public hearings (a `PUBLIC HEARING(S)` section's items, or a "PUBLIC HEARING ON ..." heading); patterns are tuned against real Norwalk agendas saved in `tests/fixtures/civicclerk_agendas/`. The meeting post then adds "🗣 Public comment on the agenda" and/or a hearing line: "🗣 Public hearing: <topic>" only for a single hearing whose topic is ≤ 60 characters, else "🗣 Public hearing" / "🗣 Public hearings (N)". Topics are never truncated; the topic, then the hearing line, then the whole note is dropped to keep the post within `Post.MAX_POST_LENGTH`. Best effort: a missing, unpublished or unreadable agenda (transient errors are retried like the meetings read) posts the meeting without the note. `NorwalkFeeds.get_meeting_reminders` replies to the day's meeting post with the start time and Zoom link from the first run within 75 minutes of the start (skipped for cancelled meetings, or if the day's meeting post isn't found)

**Minutes**: `NorwalkFeeds.get_meeting_minutes` posts "Minutes posted: <meeting>, <date>" with a card for the minutes file (`{PORTAL_URL}/event/{id}/files/agenda/{file_id}`; the `Minutes` record keeps `file_id` for `get_file_text`). Every run reads meetings from the last 60 days (`MINUTES_LOOKBACK`; Norwalk's median lag is ~5 days, 90% within 16) in one paged `iter_events` call and posts minutes published in the last 3 days (`MINUTES_FRESHNESS`, so a first run doesn't post the backlog). Minutes files published before their meeting are the previous meeting's and are ignored. Each is a reply to the meeting's post from the day of the meeting, else the day before (`Post.otherwise` chains the alternatives), else posted standalone when the meeting post has left the 30-day history

**Meeting recordings**: `NorwalkFeeds.get_meeting_recordings` reads the last 14 days of meetings (`norwalk_civic_clerk.get_recent_meetings`, `RECORDING_LOOKBACK_DAYS`) and replies to each one's post (from its day, else the day before, via `Post.otherwise`) with a "Recording: <name>" YouTube card. Replies only: if neither meeting post is in the history the recording is skipped. Norwalk leaves `youtubeVideoId` empty and puts a `youtu.be` link in the raw `externalMediaUrl` once the recording is up (before that it may hold the Zoom link, which is ignored). The thumbnail is `i.ytimg.com`'s (YouTube's watch page bot-checks datacenter IPs, so og:image is unreliable)

**Board of Education**: `src/rss_post/norwalk_boe.py` - Today's and tomorrow's BoE and committee meetings from the district's Finalsite BOE Calendar iCal feed (they aren't in the city's CivicClerk). The feed has no page per meeting, so each card links to the calendar's `~occur_id/<id>` deep link (unique per meeting; like city meetings, posts are keyed by that link + the posting day); the post links to BoardDocs for agendas but never reads it (BoardDocs refuses automated requests)

**Norwalk Oyster**: `src/rss_post/norwalk_oyster.py` - Neither source has a usable RSS feed: events come from the calendar's JSON data file (top-rated, scored 4+, posted at 7 AM), and newsletter issues from the beehiiv newsletter's Google News sitemap (its `/feed` is behind a Cloudflare bot check)

**Norwalk Public Library**: `src/rss_post/norwalk_library.py` - Events from the library's LibraryCalendar JSON feed (`norwalkpl.librarycalendar.com/events/feed/json`, main library and South Norwalk branch). Filtered to community and adult programs: children-only age groups, storytimes/play groups, recurring series (title appearing more than 3 times in the feed), closures, cancellations and outside groups' room bookings (no program type, description or image) are skipped, repeat sessions of a program on one day collapse to one, and at most 3 a day are posted (featured first)

**City community calendars**: `src/rss_post/norwalk_city_calendar.py` - The city's CivicPlus RSS feeds for the Recreation & Parks and Main City calendars, read for each event's date, times and place; posted on the day like Norwalk Oyster events, at most 2 a day per calendar (`CITY_EVENTS_PER_DAY`), skipping events Norwalk Oyster also posts that day (matched by name)

**LLM client**: `src/rss_post/summarize.py` - `summarize(text, instructions, max_chars)` over any OpenAI-compatible Chat Completions API (plain httpx), for AI features to share. Returns None on any failure and never raises. The first request of a run doubles as the check that the key works: if it is rejected (4xx other than 429), times out or can't connect, the LLM is off for the rest of the run (one warning, no more calls); after a success, a timeout/5xx/refusal/empty or overlong answer skips only that summary. 20 s timeouts and 60 s of LLM time per run at most. LLM problems never count as run failures

**Agenda summaries**: `NorwalkFeeds.get_agenda_summaries` - an "AI summary of the agenda:" reply (2–3 bullets) under a city meeting's post, only for meeting posts made in the same run, so the LLM is asked once per meeting post (normally once per meeting, under the day-before post; again under the on-the-day post only if the day-before one has no summary). The agenda text is `meeting["agenda_outline"]` (`norwalk_civic_clerk.agenda_outline`), built from the same agenda read that finds public comment (`get_todays_meetings(read_agendas=True)`), so summaries make no CivicClerk requests of their own; unpublished or boilerplate-only agendas get no outline and no call. An answer longer than `AGENDA_SUMMARY_MAX_CHARS` is cut to its leading whole bullets (`fit_bullets`) rather than dropped. The source catches everything itself, so it never adds to the run's failures

**Weekly look-ahead**: `src/rss_post/lookahead.py` - On Sundays from 6 PM Eastern (first successful run at or after `LOOKAHEAD_HOUR`), a thread for the coming Mon–Sun: a root post ("This week in Norwalk: N meetings, M events", linking the CivicClerk portal, #NorwalkCT) and one reply per day with something on, in time order, each replying to the one before. Meetings (CivicClerk, Board of Education) are all listed; events (Norwalk Oyster, library, city calendars; each module's own filters/caps per day, same-event listings merged, and a city-calendar event dropped when it lists a CivicClerk meeting that day: similar name after dropping dates/"special meeting"/"public hearing" noise, start within 30 min) are capped at `MAX_EVENTS_PER_DAY` with "+N more". Meetings are never merged. Names are tidied (whitespace, space before commas, trailing dates). Replies may use up to `REPLY_MAX_LENGTH` (295 code points, under Bluesky's 300 graphemes; links are facets on the names, so URLs don't count); a busy day is split into "Monday (part 2), ..." replies. Each source is read once for the week (`get_meetings_between`, `get_events_by_day` helpers). The root is keyed by the portal link and the Sunday it went out; each day's reply is its own reply kind (`dedup.lookahead_kind`, prefixes in `REPLY_PREFIXES`), so hourly re-runs skip what went out and a run that fails halfway is resumed by the next. A source that fails is left out of the thread (logged); if all fail, nothing is posted and the run reports a failure

**Deduplication**: `src/rss_post/dedup.py` - Reads the links the account posted (and its own replies) in the last 30 days from Bluesky at startup and skips items already posted (no local storage). `published_posts` maps each key to the latest post's `PostRef`, and `SeenPosts.ref(key_or_post)` looks a post up so later runs can reply to it. Replies are keyed by their thread root's URI and kind (`reply_key`), so each follow-up goes out once per thread; a reply's kind is read back from its text's fixed prefix (`REPLY_PREFIXES`, e.g. reminder = "Starting at"), and new kinds register theirs there. A reply with a link card is also keyed by the card's link, so its item isn't later posted standalone

**Logging**: `src/rss_post/logging_config.py` - Centralized logging configuration with structured output

### Configuration

Environment variables in `.env` file (template at `template.env`):
- `BSKY_USERNAME` - Bluesky username
- `BSKY_PASSWORD` - Bluesky password
- `LLM_API_KEY` - API key for AI agenda summaries; unset turns them off (nothing else changes). Never log it
- `LLM_BASE_URL` - OpenAI-compatible API base URL (default Gemini's `https://generativelanguage.googleapis.com/v1beta/openai/`)
- `LLM_MODEL` - model name (default `gemini-3.5-flash-lite`)

### Key Patterns

- Builder pattern for post construction with length validation
- Time-based filtering (only posts from within specified hours)
- Link-based deduplication against the account's own recent posts; replies deduped by (thread root, kind)
- Threaded follow-ups (meeting reminders, recordings, minutes and agenda summaries) found via the post history, not local storage
- Content filtering (CT Mirror posts filtered for "norwalk" mentions)
- Schedule-aware posting (meetings, Board of Education meetings, Norwalk Oyster events, library events and city calendar events posted once a day by the first successful run from 7 AM Eastern; ones that have started are skipped)
- Weekly look-ahead thread on Sunday evenings, deduped per week by its root's daily key and per day by reply kind
- Adapter pattern for output destinations (Bluesky vs console)
- Comprehensive logging throughout all components with configurable levels
- Per-source and per-post error isolation; the run exits 1 if anything failed
