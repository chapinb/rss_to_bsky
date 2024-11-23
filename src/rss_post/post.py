from atproto import client_utils


class Post:
    def __init__(self):
        self.text_builder = client_utils.TextBuilder()

    def from_feed(self, feed_title):
        self.text_builder.text(feed_title)
        return self

    def with_title(self, item_title):
        self.text_builder.text(item_title)
        return self

    def with_description(self, item_description):
        self.text_builder.text(item_description)
        return self

    def with_link(self, item_link):
        self.text_builder.text(item_link)
        return self

    def build(self):
        return self.text_builder
