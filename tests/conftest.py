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
