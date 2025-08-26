# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This project uses `uv` as the package manager and build tool.

### Development Setup
- `uv sync` - Install dependencies and set up development environment
- `uv run pytest` - Run tests
- `uv run pytest tests/test_specific.py` - Run a single test file
- `uv run pytest-watcher` - Run tests continuously during development

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
- Nancy on Norwalk blog posts
- CT Mirror stories (filtered for Norwalk mentions)
- City meeting schedules via web scraping

**Post Building**: `src/rss_post/post.py:Post` - Builder pattern for constructing social media posts with length constraints (250 chars), HTML stripping, and link formatting

**Output Adapters**: `src/rss_post/bsky.py`
- `Bluesky` class - Posts to actual Bluesky API using atproto
- `Stdout` class - Dry-run mode that prints to console

**RSS Processing**: `src/rss_post/read_rss.py` - Thin wrapper around feedparser library

**Web Scraping**: `src/rss_post/norwalk_civic_clerk.py` - Selenium-based scraper for meeting information

**Logging**: `src/rss_post/logging_config.py` - Centralized logging configuration with structured output

### Configuration

Environment variables in `.env` file (template at `template.env`):
- `BSKY_USERNAME` - Bluesky username
- `BSKY_PASSWORD` - Bluesky password

### Key Patterns

- Builder pattern for post construction with length validation
- Time-based filtering (only posts from within specified hours)
- Content filtering (CT Mirror posts filtered for "norwalk" mentions)
- Schedule-aware posting (meetings only posted at 7 AM)
- Adapter pattern for output destinations (Bluesky vs console)
- Comprehensive logging throughout all components with configurable levels
- Error handling and graceful degradation in RSS parsing and web scraping
