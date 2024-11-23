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
