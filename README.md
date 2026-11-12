# rss_to_bsky

The bot behind [@norwalkctevents.bsky.social](https://bsky.app/profile/norwalkctevents.bsky.social).
Every run posts new items from these sources to Bluesky:

| Source | How it's read |
| --- | --- |
| City of Norwalk news flashes | RSS |
| City of Norwalk board/commission calendar | RSS |
| Norwalk Current (formerly Nancy on Norwalk) | RSS |
| CT Mirror stories that mention Norwalk | RSS, filtered |
| Today's and tomorrow's city meetings (each once a day, from the first run at or after 7 AM Eastern) | CivicClerk API via [libmuni](https://github.com/chapinb/libmuni) |
| Today's and tomorrow's Board of Education and committee meetings (each once a day, from the first run at or after 7 AM Eastern; ones that have started are skipped) | The [Norwalk Public Schools BOE Calendar](https://www.norwalkps.org/board-of-education1/boe-calendar)'s iCal feed |
| [Norwalk Oyster](https://norwalkoyster.com/) newsletter issues | The newsletter's Google News sitemap |
| Today's top-rated [Norwalk Oyster](https://norwalkoyster.com/) events (from the first run at or after 7 AM Eastern; ones that have started are skipped) | The calendar's JSON data, events scored 4+ of 5 |

RSS items are posted as link cards when they were published within the last
`<hours>` hours and the account hasn't already posted that link in the last 30
days (a cancellation of an already-posted item is posted again, once). Run the bot at least once every `<hours>` hours; a window longer than
the run interval is safe and lets a run catch up after a failed one.

## Running

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run norwalk 1 --dry-run   # print what would be posted in the last hour
uv run norwalk 1             # post to Bluesky
```

Options: `--dry-run` / `-n` prints instead of posting, and `--log-level`
takes `DEBUG`, `INFO` (the default), `WARNING`, `ERROR` or `CRITICAL`.

The command exits with status 1 if any source failed to load or any post
failed to send. Everything else still gets posted. If the account's recent
posts can't be read, nothing is posted (to avoid duplicates) and it exits 1.

Dry runs also skip already-posted links when `BSKY_USERNAME` is set; they read
the account's public posts, so no password is needed.

### Credentials

Copy `template.env` to `.env` (or set the variables in the environment):

```sh
BSKY_USERNAME=norwalkctevents.bsky.social
BSKY_PASSWORD=<a Bluesky app password, not the account password>
```

### AI agenda and minutes summaries (optional)

With `LLM_API_KEY` set, the bot replies under each city meeting's post with a
short "AI summary of the agenda:" (2–3 bullets written by an LLM from the
published agenda), and under each "Minutes posted" post with an "AI summary of
the minutes:" (2–3 bullets on what was decided). It works with any OpenAI-compatible Chat Completions API;
the defaults use Google Gemini:

```sh
LLM_API_KEY=<API key>                # unset: no summaries, nothing else changes
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-3.5-flash-lite
```

The LLM is asked once per meeting post, i.e. normally once per meeting (under
the post the day before), and never for agendas that aren't published yet or
have only routine items. Summaries are best effort: if the key is rejected or
the API can't be reached, the run skips summaries (one warning) and carries on;
LLM problems never make the run fail. Minutes are summarized once, by the run
that posts them, and not when the city's file has no extractable text (e.g. a
scanned PDF). Dry runs without a key log the agendas and minutes that would be
summarized.

### Docker

```sh
docker build -t rsstobsky .
docker run --rm --env-file .env rsstobsky norwalk 1
```

The image installs exactly the versions in `uv.lock`. Pushes to `main` publish
`ghcr.io/chapinb/rsstobsky:latest`.

## Development

```sh
uv run pytest               # tests
uv run pytest-watcher .     # re-run tests on change
uvx prek run --all-files    # lint/format hooks (prek install to run on commit)
```

Tests run against saved copies of the real feeds and API responses in
`tests/fixtures/`. See [docs/MODERNIZATION.md](docs/MODERNIZATION.md) for the
maintenance plan.
