from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from atproto import client_utils, models
from bs4 import BeautifulSoup

from rss_post.logging_config import get_logger

if TYPE_CHECKING:
    from rss_post.dedup import PostRef

logger = get_logger(__name__)


class Post:
    MAX_POST_LENGTH = 250
    LINK_LENGTH = 23
    # Bluesky clients clamp card descriptions to a few lines; anything past
    # this is never seen, so cut it cleanly instead of mid-word.
    MAX_CARD_DESCRIPTION_LENGTH = 300

    # WordPress feed boilerplate that is noise on a card.
    _FEED_FOOTER = re.compile(r"\s*The post .+? appeared first on .+?\.?\s*$")
    _READ_MORE = re.compile(r"\s*(\[(…|\.\.\.|&hellip;)\]|…|\.\.\.)\s*$")

    def __init__(self):
        self.text_builder = client_utils.TextBuilder()
        self.post_length = 0
        self.embed = None
        # Image the feed supplied for the card (a URL, or a packaged image
        # file); adapters fall back to the linked page's og:image when None.
        self.thumb_url: str | Path | None = None
        # The item this post is about, used to skip items already posted.
        self.link: str | None = None
        self.title: str | None = None
        # Set for posts that may go out once per day (see ``keyed_by``).
        self.day: date | None = None
        # Replies (see ``replying_to``/``reply_to``): the kind of follow-up,
        # what it replies to by key or Post (resolved when publishing), and
        # the resolved root/parent refs that go on the record.
        self.reply_kind: str | None = None
        self.reply_root: str | Post | None = None
        self.reply_parent: str | Post | None = None
        self.reply: models.AppBskyFeedPost.ReplyRef | None = None

    def from_feed(self, feed_title):
        self.text_builder.text(feed_title + "\n")
        self.post_length += len(feed_title) + 1
        return self

    def with_title(self, item_title):
        self.title = item_title
        self.text_builder.text(item_title + "\n")
        self.post_length += len(item_title) + 1
        return self

    def with_headline(self, headline: str):
        """Post text that is just the headline, for posts carried by a card.

        Gives card-only posts searchable, screen-reader-friendly text.
        """
        headline = self.remove_html_formatting(headline)
        if len(headline) > self.MAX_POST_LENGTH:
            headline = headline[: self.MAX_POST_LENGTH - 1].rsplit(" ", 1)[0] + "…"
        self.text_builder.text(headline)
        self.post_length += len(headline)
        return self

    def with_description(self, item_description, num_links: int = 0):
        self.post_length += num_links * self.LINK_LENGTH

        item_description = self.truncate_description(
            self.remove_html_formatting(item_description)
        )

        self.text_builder.text(item_description)
        return self

    def truncate_description(self, item_description):
        self.post_length += 1  # For the newline character

        if self.post_length + len(item_description) > self.MAX_POST_LENGTH:
            logger.debug(
                f"Truncating description from {len(item_description)} chars (total would be {self.post_length + len(item_description)})"
            )
            self.post_length += 3  # for the ellipsis

            max_length = self.MAX_POST_LENGTH - self.post_length
            truncated = item_description[:max_length].rsplit(" ", 1)[0]
            self.post_length += len(truncated)

            item_description = truncated + "...\n"

        return item_description + "\n"

    def with_link(self, link_text, item_link):
        self.post_length += self.LINK_LENGTH
        self.text_builder.link(link_text, item_link)
        return self

    def with_embed_card(
        self,
        url: str,
        title: str,
        description: str = "",
        thumb_url: str | Path | None = None,
    ):
        """Add an embed card with external link to the post"""
        self.embed = models.AppBskyEmbedExternal.Main(
            external=models.AppBskyEmbedExternal.External(
                uri=url,
                title=self.remove_html_formatting(title),
                description=self.card_description(description),
            )
        )
        self.link = url
        self.thumb_url = thumb_url
        logger.debug(f"Added embed card for URL: {url} (thumb: {thumb_url})")
        return self

    def keyed_by(self, link: str, day: date | None = None):
        """Skip this post if ``link`` was already posted (on ``day``, if given).

        Card posts are keyed by their card's link automatically; this is for
        posts whose item is a link in the text, such as a meeting's agenda.
        """
        self.link = link
        self.day = day
        return self

    def with_text(self, text: str):
        """Plain text, as is."""
        self.text_builder.text(text)
        self.post_length += len(text)
        return self

    def replying_to(
        self,
        root: str | Post,
        kind: str,
        parent: str | Post | None = None,
    ):
        """Make this the ``kind`` follow-up (e.g. ``"reminder"``) in a thread.

        ``root`` and ``parent`` (default: ``root``) name the posts to reply
        to: a dedup key from the account's history (``SeenPosts.key``, e.g.
        a meeting's agenda link on a day), or a ``Post`` sent earlier in the
        same run. ``publish`` resolves them and skips the reply if either
        can't be found. Each thread gets one reply of each kind.
        """
        self.reply_kind = kind
        self.reply_root = root
        self.reply_parent = parent
        return self

    def reply_to(self, root: PostRef, parent: PostRef | None = None):
        """Reply to known posts: ``parent`` (default: ``root``) in ``root``'s thread."""
        self.reply = models.AppBskyFeedPost.ReplyRef(
            root=root.strong_ref(), parent=(parent or root).strong_ref()
        )
        return self

    @classmethod
    def card_description(cls, description: str) -> str:
        """Plain-text card description without feed boilerplate, length-capped."""
        text = cls.remove_html_formatting(description)
        text = cls._FEED_FOOTER.sub("", text)
        text, read_more = cls._READ_MORE.subn("", text)
        if len(text) > cls.MAX_CARD_DESCRIPTION_LENGTH:
            text = text[: cls.MAX_CARD_DESCRIPTION_LENGTH - 1].rsplit(" ", 1)[0]
            read_more = 1
        if read_more and text:
            text = text.rstrip(" ,;:-—") + "…"
        return text

    def build(self):
        logger.debug(f"Built post with final length: {self.post_length}")
        return self.text_builder

    @staticmethod
    def remove_html_formatting(text: str) -> str:
        return " ".join(BeautifulSoup(text, "html.parser").stripped_strings)
