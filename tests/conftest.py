from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def freeze_feeds_now():
    """Freeze ``datetime.now`` as seen by ``rss_post.norwalk_feeds``."""
    patchers = []

    def _freeze(now: datetime):
        patcher = patch("rss_post.norwalk_feeds.datetime", wraps=datetime)
        mock_datetime = patcher.start()
        mock_datetime.now.return_value = now
        patchers.append(patcher)
        return mock_datetime

    yield _freeze
    for patcher in patchers:
        patcher.stop()


@pytest.fixture(autouse=True)
def no_thumbnail_network():
    """Keep tests offline: thumbnail lookups fail unless a test patches them."""
    import httpx

    with patch(
        "rss_post.media._get", side_effect=httpx.ConnectError("network disabled")
    ) as mock_get:
        yield mock_get


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """No LLM unless a test sets ``LLM_API_KEY``, and a fresh run's LLM state."""
    from rss_post import summarize

    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.setenv(name, "")
    summarize.reset()
    yield
    summarize.reset()
