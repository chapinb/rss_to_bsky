# Modernization Spec

A short, phased plan for bringing `rss_to_bsky` up to date without breaking the
live bot (<https://bsky.app/profile/norwalkctevents.bsky.social>). Each phase is
one or more small PRs that can ship and be rolled back independently.

## Ground rules

- **One concern per PR.** Dependency bumps, refactors and behaviour changes never
  share a PR.
- **Tests before changes.** Anything that changes what gets posted needs a test
  built from real, saved source data (`tests/fixtures/`) that fails before the
  fix and passes after.
- **CI must be green** (`prek` hooks + `pytest` on the locked dependencies)
  before merging.
- **Dry-run before deploy.** Any PR that changes posting behaviour gets a
  `uv run norwalk 24 --dry-run` comparison against the previous build before the
  container is updated.
- `uv.lock` is the source of truth for dependency versions — locally, in CI, and
  in the Docker image.

## Baseline (Nov 2026)

Findings from reviewing the code and the bot's last 100 posts:

| Problem | Impact |
| --- | --- |
| CI workflow on deprecated `@v2` actions; pre-commit fails on an unused import | No safety net |
| ~20% of posts are duplicates (city feeds label EDT times as `-0500`; updated items get new `pubDate`s; same story in two feeds) | Visible spam |
| CivicClerk scraper selectors no longer match the site; failure is silent | No meeting posts at all |
| Docker image installs with `pip install .`, ignoring `uv.lock`; geckodriver URL is fragile | Untested versions in production |
| urllib3 2.2.3 / soupsieve 2.6 have published advisories | Security |
| One malformed item or failed post aborts the whole run | Missed posts |
| Card-only posts with empty text, no thumbnail, raw HTML in calendar descriptions | Low reach, messy output |

## Phases

Done phases note what actually shipped where it differs from the plan.

### Phase 1 — CI green ✅
- Modern GitHub Actions (`checkout@v7`, `astral-sh/setup-uv`), `uv sync --locked`.
- Update pre-commit hooks; fix the lint findings.

### Phase 2 — Characterization tests ✅
- Saved fixtures of real source data; tests for feed → post generation, the
  output adapters, and meeting parsing.
- Known bugs recorded as tests (future-dated items pass the posting window).

### Phase 3 — Restore meeting posts ✅
- First fixed the scraper's selectors; then (with Phase 7) replaced the
  Selenium scraper entirely with the CivicClerk API via
  [libmuni](https://github.com/chapinb/libmuni). No browser, no CSS selectors.
- Meetings without a Zoom link no longer crash the run; "today" is US/Eastern;
  times carry the correct EST/EDT label.

### Phase 4 — Stop duplicate posts ✅
- Each run reads the links the account posted in the last 30 days back from
  Bluesky (no storage needed) and skips feed items whose link is among them,
  and items already posted earlier in the same run. Links are compared after
  normalizing scheme, `www.`, trailing slash, fragment and `utm_*` parameters.
- Cancellations still go out: an item is keyed by its link plus whether its
  title says "cancelled"/"canceled", so a meeting is posted once when
  announced and once more when cancelled. Other edits (new date, new title)
  are not reposted.
- Dry runs read the same history from the public AppView, so they show what a
  real run would post.
- If the history can't be read, the run posts nothing and exits 1 rather
  than risk reposting.
- Meeting posts (no link card) are not deduplicated, on purpose: tomorrow's
  meetings are posted the day before and again on the day.
- Replaying the bot's real Oct 27 – Nov 12 history (`tests/fixtures/
  bsky_author_feed.json`) keeps 83 of 100 posts: 81 distinct links plus the
  cancellations of EID=17256 and 17257, dropping the 17 repeats.
- Follow-up, after a day or two in production: widen the posting window
  beyond the run interval (e.g. run hourly with `norwalk 6`) so a failed run
  or outage is caught up by the next one instead of losing posts.

### Phase 5 — Security patches ✅
- Lockfile-only patch of flagged packages; the rest (cryptography, h11) were
  capped by old atproto/httpx and cleared by Phase 7. `pip-audit` is clean.

### Phase 6 — Reproducible Docker image ✅
- `uv sync --locked` from the official uv image; no Firefox/geckodriver/Xvfb;
  non-root user; default `CMD ["norwalk", "1"]` (overridable as before).
- Verified with a local build + `--dry-run` against live feeds.

### Phase 7 — Dependency upgrades ✅
- Everything on current releases (atproto 0.0.73, bs4 4.15, feedparser 6.0.14,
  pytest 9, ...). libmuni replaced selenium.
- Dropped the direct `httpx` dependency; replaced `pytz` with `zoneinfo` + `tzdata`.

### Phase 8 — Resilience ✅
- Failures isolated per source and per post; the run exits 1 if anything failed.
- Feed fetch errors raise instead of looking like an empty feed, and are
  retried (norwalkct.gov resets ~1 in 6 connections).
- CivicClerk network errors, 429s and 5xx are retried too; if meetings still
  can't be read the source fails (exit 1) instead of posting nothing silently.
- Dates from feedparser's normalized `published_parsed`/`updated_parsed`;
  undated items are skipped.

### Phase 9 — Post quality (partly done)
- ✅ Strip HTML from embed card titles/descriptions.
- ✅ Read Norwalk Current (formerly Nancy on Norwalk) directly.
- ⬜ Put the title in the post text (searchable, readable in timelines).
- ⬜ Fetch `og:image` thumbnails for cards where available.

### Phase 10 — Housekeeping (partly done)
- ✅ README, `pyproject.toml` description, Python 3.14 runtime.
- ✅ Weekly Dependabot updates for uv, GitHub Actions and Docker.
- ⬜ Confirm `BSKY_PASSWORD` is a Bluesky app password.
