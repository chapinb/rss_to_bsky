# Minutes summaries: retry until the text is there

## Goal

Almost no minutes get an AI summary. The job asks CivicClerk for a minutes file's text once, in the run that posts the minutes, and CivicClerk returns an empty body until it has extracted the text some time after publishing. A check on 2026-11-27 found 28 minutes logged as "0 chars"; 27 of them have 3–29k chars from the API now, and of the 37 minutes files from the last 30 days only one (16462, Police Commission) is a scan with no text layer. Residents following @NorwalkCT minutes threads should get a summary for nearly every set of minutes.

## Requirements

- Every run tries to summarize each set of minutes still in `MINUTES_FRESHNESS` (3 days) whose minutes post exists (made this run or earlier, found in the post history) and that has no minutes summary yet.
- A minutes summary that already exists in the post history is never asked of the LLM again and never posted twice.
- When the API gives no text (or under `MIN_MINUTES_CHARS`), the minutes PDF is downloaded and its text layer is extracted locally, and that text is used instead.
- A file with no text from either source is skipped and logged, and is tried again next run while it is fresh.
- Summaries still never fail a run, and the threading stays as it is: in the meeting post's thread when the minutes replied to it, else in the minutes post's own thread.
- The docs (CLAUDE.md, the log message) say that empty API text usually means "not extracted yet", not "scanned".

## Out of scope

- OCR of scanned PDFs (one in 37; not worth a dependency or a service). Paperless-ngx was considered and rejected: see Decision log.
- Agenda summaries, which already work.
- Changes to libmuni itself (its `get_file_text` docstring is noted under Deferred).

## Constraints

- PDF text extraction uses `pypdf` (pure Python wheel, so the Dockerfile needs no apt packages).
- PDFs come from the city's CivicClerk API, but parsing is still bounded: read at most 20 MB from the stream (`open_file`), and extract at most 100 pages. Over the size cap or on any pypdf error, the fallback gives no text (logged), never an exception out of the summary source.
- The extracted text goes through the same `plain_text`, `minutes_digest.condense` and `summarize` path (nonce-tagged untrusted input) as API text.
- CivicClerk load per run: one text request per pending minutes file, plus one download only when the API text is empty. Requests stay paced by the client (1/s).
- Retries are hourly for the freshness window, so a persistently refused LLM answer is asked at most ~72 times; acceptable given `summarize`'s per-run 60 s budget and that it switches itself off for the run on auth/connection failures.
- Commit format and `/commit` per the user's global CLAUDE.md; TDD for each behavior slice.

## Environment

- Tests: `uv run pytest -q` in `/home/chapin/dev/rss_to_bsky`.
- Lint/format: pre-commit (ruff) via `.pre-commit-config.yaml`.
- Dry run: `uv run norwalk 24 --dry-run`.
- Deploy: push `main` to GitHub (`build.yaml` pushes `ghcr.io/chapinb/rsstobsky:latest`), pin the new digest in `agcb-home-kubernetes/services/rss-to-bsky/cronjob.yaml`, push, then sync the `rss-to-bsky` Argo CD app (nothing syncs automatically).

## Decision log

- 2026-11-27: Retry from the post history instead of only in the posting run, because the text appears hours after publishing; the history already holds the minutes post (card link key) and any summary (`reply_key(root, "minutes-summary")`), so no local state is needed.
- 2026-11-27: Fall back to local extraction immediately when the API text is empty, rather than waiting some hours first: 36 of 37 PDFs have a text layer, and one small download beats a summary that comes hours late. The API text stays first because it costs no download.
- 2026-11-27 (slice 1): Two tests encoded the old "asked once, never retried" requirement (`test_failed_minutes_summary_is_not_retried_by_later_runs`, and the end of the meeting-thread summary test); they were rewritten to the new requirement, not relaxed. A minutes post whose thread root can't be found is treated as summarized (skipped), since its reply couldn't be threaded anyway.
- 2026-11-27: No OCR, and no Paperless-ngx. Paperless would solve the scans with a dedicated, permission-limited user, but it adds an async upload-and-poll step to the hourly run and couples a public bot to the private archive, all for about one file in 37.

## Deferred

- libmuni's `get_file_text` docstring should say an empty body often means the text isn't extracted yet.
- If scans turn out more common than 1 in 37, revisit OCR.

## Done criteria

- All tests pass; new tests cover a summary made on a later run from the history, no repeat when the summary exists, and the PDF fallback (including the size cap and a parse error).
- A dry run against the live API shows minutes summaries being prepared for fresh minutes.
- The new image runs in the cluster and its logs show minutes text read (API or PDF) with no new warnings.

## Slices

- [x] 1. Tracer: summarize fresh minutes posted on an earlier run, found via the history, skipping ones already summarized (tests in `tests/test_minutes.py`; `norwalk_feeds.py`).
- [ ] 2. PDF fallback: when API text is empty or short, download and extract with pypdf, bounded by size and pages (tests in `tests/test_norwalk_civic_clerk.py`; `norwalk_civic_clerk.py`, `pyproject.toml`, `uv.lock`).
- [ ] 3. Docs: CLAUDE.md minutes-summaries paragraph and the "scanned, or too short" log wording (`CLAUDE.md`, `norwalk_feeds.py`).
- [ ] 4. Deploy: push, pin the image digest in the k8s repo, sync, and check the next run's logs.
