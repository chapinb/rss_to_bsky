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
- **CI must be green** (`pre-commit` + `pytest` on the locked dependencies)
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

### Phase 1 — CI green ✅
- Modern GitHub Actions (`checkout@v7`, `astral-sh/setup-uv`), `uv sync --locked`.
- Update pre-commit hooks; fix the lint findings.

### Phase 2 — Characterization tests ✅
- Saved fixtures of the real City of Norwalk feed and CivicClerk portal page.
- Tests for feed → post generation, the output adapters, and meeting parsing.
- The CivicClerk scraper is split into *fetch* (Selenium) and *parse* (pure
  function) so parsing is testable without a browser.

### Phase 3 — Restore meeting posts ✅
- Update CivicClerk selectors (`li.meeting-event`) and make date matching
  case-insensitive.
- Log a warning when the page yields no meeting entries so the next site change
  isn't silent.
- Don't emit a broken link when a meeting has no media/Zoom link.
- Decide "today" in US/Eastern rather than the container's (UTC) clock.

### Phase 4 — Stop duplicate posts
- Record links already posted and skip them. This covers all three duplicate
  causes at once, independent of feed timestamp quirks.
- **Open decision:** where the record lives — a JSON/SQLite file on a mounted
  volume, or read back the bot's own recent posts from Bluesky at startup (no
  storage needed).

### Phase 5 — Security patches (lockfile only)
- `uv lock --upgrade-package urllib3 --upgrade-package soupsieve --upgrade-package pytest`.
- No code changes; tests must pass unchanged.

### Phase 6 — Reproducible Docker image
- Install with `uv sync --locked --no-dev`; pin geckodriver to an explicit
  release URL; add an `ENTRYPOINT`.
- Verify with a local build + `--dry-run` before switching the deployment.

### Phase 7 — Dependency upgrades (one PR each)
- Low risk first: beautifulsoup4, feedparser → selenium → atproto (pre-1.0, most
  likely to break).
- Drop the unused direct `httpx` dependency; replace `pytz` with `zoneinfo`.

### Phase 8 — Resilience
- Isolate failures per feed and per post (log and continue).
- Tolerant date handling via feedparser's `published_parsed`; skip undated items.

### Phase 9 — Post quality
- Put the title in the post text (searchable, readable in timelines).
- Strip HTML from and cap length of embed card descriptions.
- Fetch `og:image` thumbnails for cards where available.
- Rename the "Nancy on Norwalk" source to Norwalk Current (the feed now redirects).

### Phase 10 — Housekeeping
- README, `pyproject.toml` description, consider Python 3.12+.
- Confirm `BSKY_PASSWORD` is a Bluesky app password.
- Enable Dependabot/Renovate so the project doesn't drift again.
