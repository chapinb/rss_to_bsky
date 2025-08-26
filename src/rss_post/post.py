from atproto import client_utils
from bs4 import BeautifulSoup

from rss_post.logging_config import get_logger

logger = get_logger(__name__)


class Post:
    MAX_POST_LENGTH = 250
    LINK_LENGTH = 23

    def __init__(self):
        self.text_builder = client_utils.TextBuilder()
        self.post_length = 0

    def from_feed(self, feed_title):
        self.text_builder.text(feed_title + "\n")
        self.post_length += len(feed_title) + 1
        return self

    def with_title(self, item_title):
        self.text_builder.text(item_title + "\n")
        self.post_length += len(item_title) + 1
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

    def build(self):
        logger.debug(f"Built post with final length: {self.post_length}")
        return self.text_builder

    @staticmethod
    def remove_html_formatting(text: str) -> str:
        return " ".join(BeautifulSoup(text, "html.parser").stripped_strings)
