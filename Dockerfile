FROM python:3.11-slim AS build

# Install Firefox and geckodriver dependencies
RUN apt-get update && apt-get install -y \
    firefox-esr \
    libdbus-glib-1-2 \
    libgtk-3-0 \
    wget \
    xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && wget -q https://github.com/mozilla/geckodriver/releases/latest/download/geckodriver-v0.35.0-linux64.tar.gz \
    && tar -xvzf geckodriver-v0.35.0-linux64.tar.gz -C /usr/local/bin \
    && rm geckodriver-v0.35.0-linux64.tar.gz

# Set environment variables for Selenium
ENV MOZ_HEADLESS=1
ENV PATH="/usr/local/bin:$PATH"

# Install build dependencies
RUN pip install uv

# Install dependencies
WORKDIR /app
COPY pyproject.toml /app
RUN pip install .

# Install package
COPY . /app/
RUN pip install .
