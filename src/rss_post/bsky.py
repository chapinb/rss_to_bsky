from atproto import Client, client_utils
from decouple import config


class Bluesky:
    def __init__(self):
        self.client = Client()
        self.profile = self.client.login(
            config("BSKY_USERNAME"), config("BSKY_PASSWORD")
        )

    def post(self, post: client_utils.TextBuilder):
        post = self.client.send_post(post)


class Stdout:
    # A dry-run class that prints the post to the console
    def post(self, post: client_utils.TextBuilder):
        print(post.build_text())
