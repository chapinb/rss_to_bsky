# Base images are pinned by digest, so a build uses exactly the image
# reviewed; Dependabot's weekly PRs move the digests when they're rebuilt
# (Debian security fixes included).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder

# Some dependencies (e.g. zstandard, via atproto) may not publish wheels for
# the newest Python yet and must be compiled from source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install locked dependencies first so they are cached across source changes.
COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --locked --no-install-project --no-editable

# Install the application itself.
COPY src /app/src
RUN uv sync --locked --no-editable


FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

RUN useradd --system --no-create-home app
USER app

# Override with the desired arguments, e.g. `docker run IMAGE norwalk 1`.
CMD ["norwalk", "1"]
