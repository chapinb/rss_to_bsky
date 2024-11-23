from atproto import client_utils


class Post:
    MAX_POST_LENGTH = 300
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

        item_description = self.truncate_description(item_description)

        self.text_builder.text(item_description)
        return self

    def truncate_description(self, item_description):
        if self.post_length + len(item_description) > self.MAX_POST_LENGTH:
            # Truncate the description if it exceeds the maximum post length
            self.post_length += 3  # for the elipsis

            item_description = item_description[
                : self.MAX_POST_LENGTH - self.post_length
            ]
            item_description += "..."
        return item_description

    def with_link(self, item_link, link_text="link"):
        self.text_builder.link(link_text, item_link)
        return self

    def build(self):
        return self.text_builder
