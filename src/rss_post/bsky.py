from typing import Any

from atproto import Client, client_utils, models
from decouple import config

from rss_post import media
from rss_post.logging_config import get_logger

logger = get_logger(__name__)


def _thumbnail_url(post_obj: Any) -> str | None:
    embed = getattr(post_obj, "embed", None)
    if embed is None:
        return None
    return media.find_thumbnail_url(
        embed.external.uri, getattr(post_obj, "thumb_url", None)
    )


class Bluesky:
    def __init__(self):
        logger.info("Initializing Bluesky client")
        self.client = Client()
        username = config("BSKY_USERNAME")
        logger.info(f"Logging in to Bluesky as {username}")
        self.profile = self.client.login(username, config("BSKY_PASSWORD"))
        logger.info("Successfully logged in to Bluesky")

    def attach_thumbnail(self, post_obj: Any) -> None:
        """Upload the card's image and set it as the thumb, if one is usable.

        Never raises: a card without a thumbnail beats a missing post.
        """
        try:
            image_url = _thumbnail_url(post_obj)
            if image_url is None:
                return
            image = media.download_thumbnail(image_url)
            if image is None:
                return
            blob = self.client.upload_blob(image).blob
            post_obj.embed.external.thumb = blob
            logger.debug(f"Attached thumbnail {image_url}")
        except Exception as e:  # noqa: BLE001 - thumbnails are optional
            logger.warning(f"Posting card without thumbnail: {e}")

    def post(self, post_obj: Any) -> None:
        try:
            text_builder: client_utils.TextBuilder = post_obj.build()
            embed: models.AppBskyEmbedExternal.Main | None = getattr(
                post_obj, "embed", None
            )
            if embed is not None and embed.external.thumb is None:
                self.attach_thumbnail(post_obj)

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
        embed: models.AppBskyEmbedExternal.Main | None = getattr(
            post_obj, "embed", None
        )

        post_text: str = text_builder.build_text()
        logger.debug(f"Dry-run post: {post_text[:50]}...")
        print(post_text)

        if embed:
            print(f"[EMBED CARD: {embed.external.title} - {embed.external.uri}]")
            if embed.external.description:
                print(f"  {embed.external.description}")
            print(f"  [THUMBNAIL: {_thumbnail_url(post_obj) or 'none'}]")
