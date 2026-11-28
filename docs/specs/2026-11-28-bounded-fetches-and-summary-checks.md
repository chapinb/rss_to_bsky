# Bounded fetches and checked AI summaries

## Goal

Fix the three findings the 2026-11-28 security review (Fable) ranked first, all verified against the code. RSS fetches have no timeout, so one stalled feed hangs the hourly run until the pod deadline and loses every later source. Thumbnail and og:image fetches download whole bodies of any size from feed-chosen URLs, with no scheme, host or file-type check. AI summaries are posted as whatever text the model returns, so injected wording in an agenda or minutes could appear under "AI summary" on a civic news account.

## Requirements

- Feed fetches give up after a total time budget and a byte cap, and a failure is reported like any other unreachable feed (`FeedUnavailableError`, retried).
- Thumbnail and og:image fetches:
  - follow at most a few redirects and only to `https` URLs on public hosts (no IP literals in private, loopback or link-local ranges, no `localhost`);
  - stop reading at a byte cap and a total time budget;
  - accept an image only if its `Content-Type` and its leading bytes both say JPEG, PNG, GIF or WebP.
  Any failure leaves the card without a thumbnail, as now.
- An AI summary is posted only if every line is a "•" bullet (after the existing `-`/`*` normalization), there are at most 3 bullets, and no line has a URL, a bare domain, an `@handle` or a `#hashtag`. Otherwise it is dropped and logged; agenda and minutes summaries behave as for any other failed summary.

## Out of scope

- The other review findings (Python 3.14 vs 3.15 in CI, the Zoom link field, uncaught timezone errors, future-dated items, `condense` input cap, duplicated `fetch`/`with_retries`, the size of `norwalk_feeds.py`, dead code, CI permissions, nits). They go under Deferred.
- The existing `fetch` helpers in `norwalk_oyster.py` and `norwalk_boe.py` (they already have per-request timeouts); moving them onto the new helper is the duplication finding.
- The egress proxy and NetworkPolicy in `agcb-home-kubernetes`.

## Constraints

- One new module, `src/rss_post/http.py`, holds the bounded GET; `read_rss.py` and `media.py` use it. It raises a subclass of `httpx.HTTPError` for its own refusals (too large, too slow, refused URL), so callers' existing `except httpx.HTTPError` keep working.
- Feed bodies are fetched by the helper and handed to `feedparser.parse` as bytes, so feedparser never opens a connection itself.
- Limits: feeds 10 MB and 30 s total; pages read for og:image 2 MB and 15 s; thumbnails `MAX_THUMB_BYTES` (1 MB) and 15 s; at most 5 redirects.
- The host check is on the URL only (scheme, `localhost`, IP literals). Hostnames that resolve to private addresses can't be caught reliably here, since HTTPS goes through the egress proxy, which resolves names itself; see Deferred.
- The summary check sits in one place that both agenda and minutes summaries already pass through (`fit_bullets`), and it keeps the existing behavior of cutting overlong answers to their leading bullets.
- TDD per slice; small commits via `/commit`; pre-commit (ruff) clean.

## Environment

- Tests: `uv run --directory /home/chapin/dev/rss_to_bsky pytest -q`.
- Lint: `uvx --directory /home/chapin/dev/rss_to_bsky pre-commit run --files <files>`.
- Dry run (reads live sources, posts nothing): `uv run --directory /home/chapin/dev/rss_to_bsky norwalk 1 --dry-run`.
- Deploy: push `main`, wait for the "Build and Push Docker Image" run, take the pushed digest from its log ("pushing manifest for …@sha256:…"), pin it in `agcb-home-kubernetes/services/rss-to-bsky/cronjob.yaml`, push, `argocd --core --kube-context puck-argocd app diff rss-to-bsky --refresh`, then `app sync rss-to-bsky`.

## Decision log

- 2026-11-28: `#` and `@` are rejected only as hashtag or handle shapes (`#` or `@` followed by a letter), not as bare characters: agendas and minutes are full of "Item #3" and "Resolution #2026-14". Summaries are plain text with no facets, so neither would be clickable or notify anyone anyway; the check is against readable injected wording.
- 2026-11-28 (slice 1): `read_rss` is split into the bounded fetch and `parse_feed(content, url, headers)`, so tests (and `test_norwalk_feeds`'s fixtures) parse saved bytes instead of having feedparser open file paths. `parse_feed` defaults the content type to `application/xml`, since feedparser marks a feed given headers without one as malformed. Proxies still apply: httpx honors `HTTPS_PROXY` as urllib did.
- 2026-11-28 (automated security review, after slice 2): two valid findings, both fixed in their own commits. (1) httpx's decoder inflates each received chunk whole, so a small gzip body could expand far past the cap before the size check; `get` now asks only for gzip/deflate, refuses other encodings, and inflates with zlib's `max_length`. (2) `public_https` let internal names through when their last label looked like a TLD (`*.svc.cluster.local`, `.local`, `.lan`), and the pod's `NO_PROXY` sends those, and the home domain, straight to the internal gateway that the NetworkPolicy allows; it now refuses well-known private suffixes, every name in `NO_PROXY` (so the home domain isn't written into this public repo), and any port but 443. DNS-resolve-and-pin was not added: HTTPS goes through the proxy, which resolves names itself (Deferred).
- 2026-11-28 (slice 3): Numbered lines (`1.`, `2)`) count as bullets too. The domain pattern is generic (a word, a dot, 2+ letters) rather than a TLD list, so it also catches made-up domains; abbreviations like "p.m.", "St. Mary's" and "No. 14" pass. Checked against the account's 21 posted AI summaries: none would have been dropped.
- 2026-11-28: `https` only for feed-supplied image and page URLs: every configured source and every image URL in the saved fixtures is already `https`.

## Deferred

- Egress proxy: deny private, loopback and link-local destinations (it is log-only today), which is the only reliable guard against names that resolve to the LAN. Lives in `agcb-home-kubernetes`.
- Remaining review findings, in rough priority: CI tests on Python 3.14 while the image runs 3.15, and the build not waiting for the tests; the "Join on Zoom" link from an unvalidated field; one malformed item (unknown timezone, entry without title or link) failing a whole source; future-dated items always passing the posting window; no input cap before `minutes_digest.condense`; `fetch`/`with_retries`/`EASTERN` duplicated, and the calendar modules sending Norwalk Oyster's User-Agent; `norwalk_feeds.py`'s hidden source ordering; dead code in `post.py`, `dedup.published_keys`, `read_participation` and `logging_config`; `test.yaml` has no `permissions:` and runs an unpinned `uvx prek`; card titles have no length cap; BoE logs whole raw events; no `.dockerignore`.

## Done criteria

- All tests pass, including new ones for: a stalled or oversized feed raising `FeedUnavailableError`; thumbnail refusals (non-`https`, private IP literal, `localhost`, a redirect to one of those, oversize, wrong magic bytes); and summary answers rejected for a URL, a domain, a handle, a hashtag, a non-bullet line, and kept to 3 bullets.
- A dry run against the live sources reads every feed and still finds thumbnails.
- The new image runs in the cluster with no new warnings in its first run.

## Slices

- [x] 1. Tracer: `http.get_bounded` (byte cap, total deadline, `httpx.HTTPError` subclass) and `read_rss` fetching through it (tests in `tests/test_http.py`, `tests/test_read_rss.py`; `http.py`, `read_rss.py`).
- [x] 2. Thumbnails: `https`/host checks on every hop, caps, and magic bytes (tests in `tests/test_media.py`, `tests/test_http.py`; `media.py`, `http.py`).
- [x] 3. Summary check: bullets only, at most 3, no URL/domain/handle/hashtag (tests in `tests/test_norwalk_feeds.py` or wherever `fit_bullets` is tested; `norwalk_feeds.py`).
- [x] 4. Docs: CLAUDE.md lines on fetching and summaries (`CLAUDE.md`).
- [ ] 5. Deploy: push, pin the digest, sync, check the next run.
