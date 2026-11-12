# rss_to_bsky

The bot behind [@norwalkctevents.bsky.social](https://bsky.app/profile/norwalkctevents.bsky.social).
Every run posts new items from these sources to Bluesky:

| Source | How it's read |
| --- | --- |
| City of Norwalk news flashes | RSS |
| City of Norwalk board/commission calendar | RSS |
| Norwalk Current (formerly Nancy on Norwalk) | RSS |
| CT Mirror stories that mention Norwalk | RSS, filtered |
| Today's and tomorrow's city meetings (7 AM Eastern run only) | CivicClerk API via [libmuni](https://github.com/chapinb/libmuni) |

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
uvx pre-commit run --all-files
```

Tests run against saved copies of the real feeds and API responses in
`tests/fixtures/`. See [docs/MODERNIZATION.md](docs/MODERNIZATION.md) for the
maintenance plan.
