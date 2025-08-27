from typing import Any, Optional
from atproto import Client, client_utils, models
from decouple import config

from rss_post.logging_config import get_logger

logger = get_logger(__name__)


class Bluesky:
    def __init__(self):
        logger.info("Initializing Bluesky client")
        self.client = Client()
        username = config("BSKY_USERNAME")
        logger.info(f"Logging in to Bluesky as {username}")
        self.profile = self.client.login(username, config("BSKY_PASSWORD"))
        logger.info("Successfully logged in to Bluesky")

    def post(self, post_obj: Any) -> None:
        try:
            text_builder: client_utils.TextBuilder = post_obj.build()
            embed: Optional[models.AppBskyEmbedExternal.Main] = getattr(
                post_obj, "embed", None
            )

            post_text: str = text_builder.build_text()
            logger.debug(f"Posting to Bluesky: {post_text[:50]}...")

            response = self.client.send_post(text_builder, embed=embed)
            logger.info(f"Successfully posted to Bluesky: {response.uri}")
        except Exception as e:
            logger.error(f"Failed to post to Bluesky: {e}")
            raise


class Stdout:
    def __init__(self) -> None:
        logger.info("Initializing dry-run mode (console output)")

    def post(self, post_obj: Any) -> None:
        text_builder: client_utils.TextBuilder = post_obj.build()
        embed: Optional[models.AppBskyEmbedExternal.Main] = getattr(
            post_obj, "embed", None
        )

        post_text: str = text_builder.build_text()
        logger.debug(f"Dry-run post: {post_text[:50]}...")
        print(post_text)

        if embed:
            print(f"[EMBED CARD: {embed.external.title} - {embed.external.uri}]")
