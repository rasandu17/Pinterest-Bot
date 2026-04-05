"""
profile_reviewer.py — Fetch Instagram profile posts (photos only) for review mode.

Uses Instagram's private API directly with our existing cookies.txt.
This is fast (2-3 API calls), reliable, and downloads NO images during fetch.
Images download on-demand only when user previews or selects a post.
"""

import os
import re
import json
import logging
import requests
import http.cookiejar
import tempfile
from pathlib import Path

logger = logging.getLogger("TumblerBot.profile_reviewer")

COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "cookies.txt")

PROFILE_PATTERN = re.compile(
    r"(https?://)?(www\.)?instagram\.com/([A-Za-z0-9_.]+)/?(\?[^\s]*)?\s*$"
)

# Instagram private API headers (web client)
_IG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-IG-App-ID": "936619743392459",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_profile_url(text: str) -> bool:
    text = text.strip()
    if re.search(r"instagram\.com/(p|reel|tv|stories|explore)/", text):
        return False
    return bool(PROFILE_PATTERN.search(text))


def extract_username(url: str) -> str | None:
    m = PROFILE_PATTERN.search(url.strip())
    return m.group(3) if m else None


def _make_session() -> requests.Session:
    """Build a requests.Session loaded with Instagram cookies."""
    session = requests.Session()
    session.headers.update(_IG_HEADERS)

    # Try the env-configured cookies file first, then common fallbacks
    for candidate in [COOKIES_FILE, "cookies.txt", "instagram_only_cookies.txt"]:
        if candidate and Path(candidate).is_file():
            try:
                cj = http.cookiejar.MozillaCookieJar(candidate)
                cj.load(ignore_discard=True, ignore_expires=True)
                for cookie in cj:
                    if "instagram" in cookie.domain:
                        session.cookies.set(
                            cookie.name, cookie.value,
                            domain=cookie.domain, path=cookie.path,
                        )
                logger.info("Loaded cookies from: %s", candidate)
            except Exception as e:
                logger.warning("Failed to load %s: %s", candidate, e)
            break

    return session


def _get_user_id(session: requests.Session, username: str) -> str:
    """Get the numeric user ID for a username via Instagram web API or HTML fallback."""
    # Method 1: Extract from HTML (less likely to 429 on Heroku/Koyeb)
    html_url = f"https://www.instagram.com/{username}/"
    try:
        # Avoid X-Requested-With for HTML requests, it gets flagged
        html_headers = dict(session.headers)
        html_headers.pop("X-Requested-With", None)
        
        html_resp = session.get(html_url, headers=html_headers, timeout=30)
        if html_resp.status_code == 200:
            # Look for multiple variants of how IG stores user ID in the HTML
            for pattern in [r'"profile_id":"(\d+)"', r'"profile_id":(\d+)', r'"user_id":"(\d+)"', r'profilePage_(\d+)']:
                match = re.search(pattern, html_resp.text)
                if match:
                    return match.group(1)
            logger.warning(f"HTML fetch returned 200 but no ID found for @{username}.")
        else:
            logger.warning(f"HTML fetch returned {html_resp.status_code} for @{username}.")
    except Exception as e:
        logger.warning(f"HTML fetch for @{username} failed: {e}")

    # Method 2: web_profile_info API (using i.instagram.com to bypass IP blocks)
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    resp = session.get(url, timeout=30)

    if resp.status_code == 200:
        data = resp.json()
        user = data.get("data", {}).get("user")
        if user:
            return str(user["id"])

    if resp.status_code == 401:
        raise RuntimeError("Instagram requires login. Make sure cookies.txt is valid.")
    if resp.status_code == 404:
        raise RuntimeError(f"Profile @{username} not found.")
        
    resp.raise_for_status()
    raise RuntimeError(f"Could not get user info for @{username}. Server returned {resp.status_code}.")


def _fetch_feed_page(
    session: requests.Session,
    user_id: str,
    count: int = 12,
    max_id: str | None = None,
) -> dict:
    """Fetch one page of a user's feed."""
    params: dict = {"count": count}
    if max_id:
        params["max_id"] = max_id

    resp = session.get(
        f"https://i.instagram.com/api/v1/feed/user/{user_id}/",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _best_image_url(candidates: list[dict]) -> str:
    """Pick the highest-resolution image from a list of image candidates."""
    if not candidates:
        return ""
    # Sort by width descending, take the first
    sorted_c = sorted(candidates, key=lambda c: c.get("width", 0), reverse=True)
    return sorted_c[0].get("url", "")


def _parse_feed_items(items: list[dict]) -> list[dict]:
    """
    Convert raw Instagram feed items to our post dict format.

    media_type: 1 = photo, 2 = video, 8 = carousel
    """
    posts = []
    for item in items:
        media_type = item.get("media_type", 0)
        if media_type == 2:
            continue  # Skip pure video posts

        shortcode = item.get("code", "")
        caption_data = item.get("caption") or {}
        caption = caption_data.get("text", "") if isinstance(caption_data, dict) else ""
        post_url = f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""

        image_urls: list[str] = []

        if media_type == 8:
            # Carousel — iterate each media item
            for cm in item.get("carousel_media", []):
                cm_type = cm.get("media_type", 0)
                if cm_type == 2:
                    continue  # skip video slides in carousel
                candidates = cm.get("image_versions2", {}).get("candidates", [])
                url = _best_image_url(candidates)
                if url:
                    image_urls.append(url)
        else:
            # Single photo
            candidates = item.get("image_versions2", {}).get("candidates", [])
            url = _best_image_url(candidates)
            if url:
                image_urls.append(url)

        if image_urls:
            posts.append({
                "post_url": post_url,
                "image_urls": image_urls,
                "caption": caption,
                "image_paths": None,  # downloaded on-demand
            })

    return posts


def fetch_profile_post_urls(
    profile_url: str,
    batch_size: int = 5,
    post_offset: int = 0,
) -> tuple[list[dict], None]:
    """
    Fetch image URLs for a batch of photo posts using Instagram's private API.

    Fast: only 2-3 HTTP requests total, no image downloads.
    Returns (posts, None) — None because no temp dir is created.
    """
    username = extract_username(profile_url) or profile_url.strip("/").split("/")[-1]
    logger.info("Fetching posts for @%s (offset=%d, batch=%d)", username, post_offset, batch_size)

    session = _make_session()

    # Step 1: Get user ID
    user_id = _get_user_id(session, username)
    logger.info("User ID for @%s: %s", username, user_id)

    # Step 2: Paginate through the feed to collect enough photo posts
    all_photo_posts: list[dict] = []
    max_id: str | None = None
    pages_fetched = 0

    # We need at least (post_offset + batch_size) photo posts total
    need = post_offset + batch_size

    while len(all_photo_posts) < need:
        feed = _fetch_feed_page(session, user_id, count=12, max_id=max_id)
        items = feed.get("items", [])
        if not items:
            break

        all_photo_posts.extend(_parse_feed_items(items))
        pages_fetched += 1

        if not feed.get("more_available", False):
            break  # No more pages

        # Get cursor for next page
        max_id = feed.get("next_max_id")
        if not max_id:
            break

        # Safety: don't fetch more than 10 pages
        if pages_fetched >= 10:
            logger.warning("Stopped after %d pages to avoid rate limiting", pages_fetched)
            break

    logger.info("Found %d total photo posts after %d page(s)", len(all_photo_posts), pages_fetched)

    # Slice to the requested batch
    batch = all_photo_posts[post_offset: post_offset + batch_size]
    logger.info("Returning %d post(s) for this batch", len(batch))

    return batch, None


def download_images_to_files(image_urls: list[str], tmp_dir: str) -> list[str]:
    """
    Download image CDN URLs to local temp files. Called on-demand per post.
    """
    paths: list[str] = []
    for i, url in enumerate(image_urls):
        try:
            resp = requests.get(url, headers=_IG_HEADERS, timeout=60)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
            path = os.path.join(tmp_dir, f"img_{i}{ext}")
            with open(path, "wb") as f:
                f.write(resp.content)
            paths.append(path)
            logger.info("Downloaded img %d: %d bytes", i + 1, len(resp.content))
        except Exception as e:
            logger.warning("Failed to download %s: %s", url[:80], e)
    return paths
