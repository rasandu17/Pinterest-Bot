"""
downloader.py — Instagram media downloader using yt-dlp
Supports both video (reels/IGTV) and photo posts.
"""

import os
import glob
import logging
import requests
from pathlib import Path

import yt_dlp

# Use bundled ffmpeg from imageio-ffmpeg if available (no system install needed)
try:
    import imageio_ffmpeg
    _FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG_PATH = None

logger = logging.getLogger("TumblerBot.downloader")

# Instagram cookies file (optional — improves success rate for private/login-walled content)
COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "")  # path to a Netscape cookies.txt

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _cookie_opts() -> dict:
    """Return cookie-related yt-dlp options."""
    opts: dict = {}
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
        logger.info("Using cookies file: %s", COOKIES_FILE)
    browser = os.getenv("INSTAGRAM_BROWSER", "").lower().strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
        logger.info("Using cookies from browser: %s", browser)
    return opts


def _ydl_opts(output_dir: str) -> dict:
    """Build yt-dlp options for Instagram video downloads."""
    outtmpl = os.path.join(output_dir, "%(id)s.%(ext)s")

    opts: dict = {
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": False,
        "writeinfojson": True,
        "http_headers": _HTTP_HEADERS,
        "socket_timeout": 60,
        "retries": 5,
        "age_limit": 99,  # attempt age-restricted content using session cookies
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }

    if _FFMPEG_PATH:
        opts["ffmpeg_location"] = _FFMPEG_PATH
        logger.info("Using bundled ffmpeg: %s", _FFMPEG_PATH)

    opts.update(_cookie_opts())
    return opts


def _download_image(url: str, output_dir: str, filename: str) -> str:
    """Download a single image URL to output_dir and return the path."""
    resp = requests.get(url, headers=_HTTP_HEADERS, timeout=60)
    resp.raise_for_status()

    # Determine extension from content type or URL
    content_type = resp.headers.get("Content-Type", "")
    if "png" in content_type:
        ext = ".png"
    elif "webp" in content_type:
        ext = ".webp"
    else:
        ext = ".jpg"

    filepath = os.path.join(output_dir, filename + ext)
    with open(filepath, "wb") as f:
        f.write(resp.content)
    logger.info("Downloaded image: %s (%d bytes)", filepath, len(resp.content))
    return filepath


def download_instagram(url: str, output_dir: str) -> tuple[str | list[str], str, str]:
    """
    Download an Instagram post/reel using yt-dlp.
    Falls back to instaloader for photo posts (supports carousels).

    Returns
    -------
    (media_path, media_type, caption)
        media_path : absolute path to the downloaded file, or a list of paths
        media_type : "video" | "image"
        caption    : post description (may be empty)
    """
    opts = _ydl_opts(output_dir)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # ── Resolve the downloaded file ────────────────────────────────────
        media_path = _find_media_file(output_dir)
        if not media_path:
            raise FileNotFoundError("yt-dlp did not produce a media file in: " + output_dir)

        ext = Path(media_path).suffix.lower()
        media_type = "video" if ext in {".mp4", ".mkv", ".webm", ".mov", ".avi"} else "image"
        caption = _extract_caption(info)
        
        logger.info("Downloaded %s (%s) — caption length: %d", media_path, media_type, len(caption))
        return media_path, media_type, caption

    except (yt_dlp.utils.DownloadError, FileNotFoundError) as e:
        err_str = str(e)
        is_restricted = any(kw.lower() in err_str.lower() for kw in [
            "Restricted Post", "unavailable for certain audiences",
            "This content may be inappropriate", "Login required",
            "Sorry, this page isn't available"
        ])

        # ── Fallback 1: gallery-dl ────────────────────────────────────────
        logger.info("yt-dlp failed — trying gallery-dl for: %s", url)
        try:
            return _download_with_gallery_dl(url, output_dir)
        except Exception as gallery_e:
            _gallery_err = gallery_e  # save before Python deletes it
            logger.warning("gallery-dl also failed: %s", gallery_e)

        # ── Fallback 2: Cobalt API (cookie-free public downloader) ────────
        logger.info("gallery-dl failed — trying Cobalt API for: %s", url)
        try:
            return _download_with_cobalt(url, output_dir)
        except Exception as cobalt_e:
            logger.exception("Cobalt API fallback also failed")
            hint = (
                "\n\n🔒 Note: This post appears to be age-restricted. "
                "Your cookies should allow access — try refreshing them via Koyeb env vars."
                if is_restricted else
                "\nMake sure the post is public and the URL is correct."
            )
            _cobalt_err = cobalt_e

        # ── Fallback 3: instagrapi (full mobile app auth) ──────────────────
        logger.info("Cobalt failed — trying instagrapi for: %s", url)
        try:
            return _download_with_instagrapi(url, output_dir)
        except Exception as insta_e:
            logger.exception("instagrapi fallback also failed")
            hint = (
                "\n\n🔒 This post is age-restricted. "
                "Add INSTAGRAM_USERNAME + INSTAGRAM_PASSWORD to Koyeb env vars to enable full access."
                if is_restricted else
                "\nMake sure the post is public and the URL is correct."
            )
            raise FileNotFoundError(
                f"All download methods failed.{hint}\n"
                f"yt-dlp: {e}\n"
                f"gallery-dl: {_gallery_err}\n"
                f"Cobalt: {_cobalt_err}\n"
                f"instagrapi: {insta_e}"
            ) from e


def _download_with_instagrapi(url: str, output_dir: str) -> tuple[str | list[str], str, str]:
    """
    4th-layer fallback using instagrapi — fully mimics the Instagram mobile app.
    Requires INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD environment variables.
    Can download age-restricted and private (if followed) content.
    """
    import re
    from pathlib import Path as _Path

    username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    password = os.getenv("INSTAGRAM_PASSWORD", "").strip()

    if not username or not password:
        raise RuntimeError(
            "instagrapi requires INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD env vars. "
            "Add them to your Koyeb environment variables!"
        )

    try:
        from instagrapi import Client
    except ImportError:
        raise RuntimeError("instagrapi is not installed. Add it to requirements.txt!")

    # Extract shortcode from URL
    match = re.search(r'/(?:reel|p)/([A-Za-z0-9_-]+)', url)
    if not match:
        raise RuntimeError(f"Could not extract shortcode from URL: {url}")
    shortcode = match.group(1)

    cl = Client()
    session_file = "instagrapi_session.json"

    # Reuse saved session to avoid repeated logins (Instagram flags rapid logins)
    if os.path.exists(session_file):
        try:
            cl.load_settings(session_file)
            cl.login(username, password)
            logger.info("instagrapi: reused saved session")
        except Exception:
            logger.warning("instagrapi: saved session expired, logging in fresh")
            cl = Client()
            cl.login(username, password)
            cl.dump_settings(session_file)
    else:
        cl.login(username, password)
        cl.dump_settings(session_file)
        logger.info("instagrapi: logged in and saved session")

    media_pk = cl.media_pk_from_code(shortcode)
    media_info = cl.media_info(media_pk)
    caption = media_info.caption_text or ""

    if media_info.media_type == 2:  # Video / Reel
        path = cl.video_download(media_pk, folder=output_dir)
        logger.info("instagrapi downloaded video: %s", path)
        return str(path), "video", caption
    elif media_info.media_type == 8:  # Carousel / multi-photo
        paths = cl.album_download(media_pk, folder=output_dir)
        str_paths = [str(p) for p in paths]
        logger.info("instagrapi downloaded album: %d items", len(str_paths))
        return (str_paths[0] if len(str_paths) == 1 else str_paths), "image", caption
    else:  # Photo
        path = cl.photo_download(media_pk, folder=output_dir)
        logger.info("instagrapi downloaded photo: %s", path)
        return str(path), "image", caption


def _download_with_cobalt(url: str, output_dir: str) -> tuple[str | list[str], str, str]:
    """Third-layer fallback using Cobalt API — free, cookie-free, no auth needed."""
    import json
    import uuid

    logger.info("Attempting Cobalt API download for: %s", url)

    # Clean the URL (remove tracking params)
    clean_url = url.split("?")[0].rstrip("/")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"url": clean_url}

    resp = requests.post(
        "https://api.cobalt.tools/",
        headers=headers,
        json=payload,
        timeout=30,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Cobalt API returned HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()

    logger.info("Cobalt API response: status=%s", data.get("status"))

    status = data.get("status")
    if status == "error":
        raise RuntimeError(f"Cobalt API error: {data.get('error', {}).get('code', 'unknown')}")

    # Cobalt returns either a direct URL or a tunnel URL
    media_url = data.get("url") or (data.get("urls") or [None])[0]
    if not media_url:
        raise RuntimeError(f"Cobalt returned no downloadable URL. Response: {data}")

    # Download the file
    filename = str(uuid.uuid4())
    ext = ".mp4" if data.get("filename", "").endswith(".mp4") else ".mp4"
    filepath = os.path.join(output_dir, filename + ext)

    dl_resp = requests.get(media_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120, stream=True)
    dl_resp.raise_for_status()

    # Detect type from Content-Type
    content_type = dl_resp.headers.get("Content-Type", "")
    if "video" in content_type:
        ext = ".mp4"
    elif "image" in content_type:
        ext = ".jpg"
    filepath = os.path.join(output_dir, filename + ext)

    with open(filepath, "wb") as f:
        for chunk in dl_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    media_type = "video" if ext == ".mp4" else "image"
    logger.info("Cobalt downloaded %s (%s)", filepath, media_type)
    return filepath, media_type, ""


def _download_with_gallery_dl(url: str, output_dir: str) -> tuple[str | list[str], str, str]:
    """Fallback downloader using gallery-dl. Supports videos, photos, and carousels."""
    import subprocess
    import json
    import glob

    # Find gallery-dl.conf in the same directory as this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    gdl_conf = os.path.join(script_dir, "gallery-dl.conf")

    cmd = [
        "python", "-m", "gallery_dl",
        "--directory", output_dir,
        "--write-metadata"
    ]

    if os.path.exists(gdl_conf):
        cmd.extend(["--config", gdl_conf])
        logger.info("Using gallery-dl config: %s", gdl_conf)

    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        cmd.extend(["--cookies", COOKIES_FILE])
    
    cmd.append(url)

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"gallery-dl failed (code {res.returncode}): {res.stderr or res.stdout}")

    # Find the downloaded files (images + json)
    # output_dir might have subdirectories created by gallery-dl, e.g. instagram/<username>/<file>
    json_files = glob.glob(os.path.join(output_dir, "**", "*.json"), recursive=True)
    image_paths = []
    caption = ""

    # Parse metadata for caption
    for j_file in json_files:
        try:
            with open(j_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                # the description field often has the caption!
                if data.get("description") and not caption:
                    caption = data["description"]
        except Exception as e:
            logger.warning("Failed to parse gallery-dl json: %s", e)

    # Find all downloaded media (videos and photos)
    for root, _, sorted_files in os.walk(output_dir):
        for f in sorted(sorted_files):
            ext = Path(f).suffix.lower()
            if ext in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".jpg", ".jpeg", ".png", ".webp"}:
                image_paths.append(os.path.join(root, f))

    if not image_paths:
        raise ValueError("No media found by gallery-dl (could be a strictly private post).")

    final_path = image_paths[0] if len(image_paths) == 1 else image_paths
    logger.info("gallery-dl downloaded %d item(s)", len(image_paths))
    
    # Determine the media type based on the first item's extension
    first_path = image_paths[0]
    first_ext = Path(first_path).suffix.lower()
    media_type = "video" if first_ext in {".mp4", ".mkv", ".webm", ".mov", ".avi"} else "image"
    
    return final_path, media_type, caption


def _find_media_file(directory: str) -> str | None:
    """Return the first media file found in *directory* (ignoring .json files)."""
    media_extensions = (
        "*.mp4", "*.mkv", "*.webm", "*.mov", "*.avi",
        "*.jpg", "*.jpeg", "*.png", "*.webp",
    )
    for pattern in media_extensions:
        matches = glob.glob(os.path.join(directory, pattern))
        if matches:
            # Prefer the largest file (avoids tiny thumbnails)
            return max(matches, key=os.path.getsize)
    return None


def _extract_caption(info: dict | None) -> str:
    """Pull the best available caption / title from yt-dlp info dict."""
    if not info:
        return ""
    return (
        info.get("description")
        or info.get("title")
        or info.get("fulltitle")
        or ""
    ).strip()
