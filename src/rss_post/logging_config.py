import logging
import sys
from typing import Optional


def setup_logging(
    level: Optional[str] = None, format_str: Optional[str] = None
) -> None:
    """
    Configure logging for the RSS to Bluesky application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_str: Custom format string for log messages
    """
    if level is None:
        level = "INFO"

    if format_str is None:
        format_str = "%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s"

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=format_str,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Set specific loggers to appropriate levels
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the specified name."""
    return logging.getLogger(name)
