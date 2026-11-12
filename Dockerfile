FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install locked dependencies first so they are cached across source changes.
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-install-project

# Install the application itself.
COPY src /app/src
RUN uv sync --locked

RUN useradd --system --no-create-home app
USER app

# Override with the desired arguments, e.g. `docker run IMAGE norwalk 1`.
CMD ["norwalk", "1"]
