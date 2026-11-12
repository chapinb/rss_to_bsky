"""Thumbnail discovery and download for link embed cards.

Everything here is best effort: a missing or broken image should leave the
card without a thumbnail, never stop the post from going out.
"""

from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

# Bluesky rejects blobs for app.bsky.embed.external thumbs above this size.
MAX_THUMB_BYTES = 1_000_000
REQUEST_TIMEOUT = 10.0
USER_AGENT = "rss-post (Bluesky link card fetcher)"

_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def _is_image(media: dict) -> bool:
    media_type = media.get("type", "")
    return media_type.startswith("image/") or media.get("medium") == "image"


def image_from_item(item) -> str | None:
    """The best image URL a feed item carries itself, if any.

    Checks, in order: ``media:content``, ``media:thumbnail``, image
    enclosures, then the first ``<img>`` in the description or content.
    """
    for media in item.get("media_content", []):
        if media.get("url") and _is_image(media):
            return media["url"]
    for media in item.get("media_thumbnail", []):
        if media.get("url"):
            return media["url"]
    for link in item.get("links", []):
        if link.get("rel") == "enclosure" and _is_image(link) and link.get("href"):
            return link["href"]

    html_parts = [item.get("description", "")] + [
        content.get("value", "") for content in item.get("content", [])
    ]
    for html in html_parts:
        img = BeautifulSoup(html, "html.parser").find("img", src=True)
        if img:
            return urljoin(item.get("link", ""), img["src"])
    return None


def _get(url: str) -> httpx.Response:
    return httpx.get(
        url,
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )


def og_image_from_page(page_url: str) -> str | None:
    """The page's ``og:image`` (or ``twitter:image``), as a site would show it."""
    try:
        response = _get(page_url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.debug(f"Could not fetch {page_url} for og:image: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    for attr, name in (
        ("property", "og:image"),
        ("name", "og:image"),
        ("name", "twitter:image"),
        ("property", "twitter:image"),
    ):
        tag = soup.find("meta", attrs={attr: name, "content": True})
        if tag and tag["content"].strip():
            return urljoin(str(response.url), tag["content"].strip())
    return None


def find_thumbnail_url(
    page_url: str, feed_image: str | Path | None
) -> str | Path | None:
    """The feed's own image when it has one, else the linked page's."""
    return feed_image or og_image_from_page(page_url)


def download_thumbnail(image_url: str | Path) -> bytes | None:
    """Fetch an image for a card thumb, or ``None`` if it is unusable.

    A ``Path`` is an image shipped with the package and is read from disk.
    """
    if isinstance(image_url, Path):
        try:
            image = image_url.read_bytes()
        except OSError as e:
            logger.warning(f"Could not read thumbnail {image_url}: {e}")
            return None
        if len(image) > MAX_THUMB_BYTES:
            logger.warning(f"Thumbnail {image_url} is over the size limit")
            return None
        return image

    try:
        response = _get(image_url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(f"Could not download thumbnail {image_url}: {e}")
        return None

    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in _IMAGE_TYPES:
        logger.warning(f"Thumbnail {image_url} is not an image ({content_type!r})")
        return None
    if len(response.content) > MAX_THUMB_BYTES:
        logger.warning(
            f"Thumbnail {image_url} is {len(response.content)} bytes, "
            f"over the {MAX_THUMB_BYTES} byte limit"
        )
        return None
    return response.content
