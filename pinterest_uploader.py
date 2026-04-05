import os
import requests
import json
import logging
import http.cookiejar

logger = logging.getLogger("PinBot.pinterest_uploader")
PINTEREST_COOKIES = os.getenv("PINTEREST_COOKIES_FILE", "pinterest_cookies.txt")

def get_session() -> requests.Session:
    """Build a requests.Session loaded with Pinterest cookies and CSRF token."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.pinterest.com/",
    })

    if os.path.exists(PINTEREST_COOKIES):
        try:
            cj = http.cookiejar.MozillaCookieJar(PINTEREST_COOKIES)
            cj.load(ignore_discard=True, ignore_expires=True)
            for cookie in cj:
                if "pinterest" in cookie.domain:
                    session.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
            logger.info("Loaded pinterest cookies.")
        except Exception as e:
            logger.warning(f"Failed to load cookies: {e}")
    else:
        logger.warning(f"{PINTEREST_COOKIES} not found.")

    # extract csrftoken
    csrftoken = session.cookies.get("csrftoken", domain=".pinterest.com")
    if not csrftoken:
        csrftoken = session.cookies.get("csrftoken", domain="www.pinterest.com")
        
    if csrftoken:
        session.headers.update({"X-CSRFToken": csrftoken})
        logger.info("Loaded CSRF token from cookies.")
    else:
        logger.warning("No CSRF token found in cookies!")

    return session

def upload_pin_from_url(session: requests.Session, board_id: str, image_url: str, title: str = "", description: str = "", link: str = "") -> dict:
    """
    Creates a Pin directly using an image URL (e.g., from Instagram CDN).
    Pinterest's backend will attempt to fetch this image.
    """
    url = "https://www.pinterest.com/resource/PinResource/create/"
    
    data = {
        "options": {
            "board_id": board_id,
            "description": description,
            "link": link,
            "title": title,
            "image_url": image_url,
            "method": "button"
        },
        "context": {}
    }
    
    payload = {
        "source_url": "/",
        "data": json.dumps(data)
    }
    
    response = session.post(url, data=payload)
    
    if response.status_code == 401:
        raise RuntimeError("Pinterest requires login. Make sure pinterest_cookies.txt is valid.")
        
    response.raise_for_status()
    return response.json()

import subprocess
import imageio_ffmpeg

def upload_local_image_and_pin(session: requests.Session, board_id: str, image_path: str, title: str = "", description: str = "", link: str = "") -> dict:
    """
    Since Pinterest's private media upload endpoints change frequently, we first 
    upload the local image to a temporary public CDN (catbox.moe) and then 
    tell Pinterest to fetch from that public URL.
    """
    if image_path.lower().endswith(".mp4") or image_path.lower().endswith(".mov") or image_path.lower().endswith(".webm"):
        logger.info(f"Video detected ({image_path}). Pinterest bans direct bot video uploads!")
        logger.info("Extracting a gorgeous high-res cover frame to use as the Pin instead...")
        cover_path = image_path + ".thumb.jpg"
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        try:
            # Extract high quality frame at the 1 second mark
            subprocess.run([
                ffmpeg_bin, "-y", "-i", image_path, "-vframes", "1", "-ss", "00:00:01",
                "-q:v", "2", cover_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            image_path = cover_path
            logger.info("Cover frame successfully extracted.")
        except Exception as e:
            logger.error(f"FFmpeg cover extraction failed: {e}")
            raise RuntimeError("Pinterest blocks direct video uploads. Bot attempted to extract a cover frame, but failed.") from e

    logger.info(f"Uploading local image to proxy CDN: {image_path}")
    
    # 1. Upload to catbox.moe (Public CDN bridge)
    try:
        url = "https://catbox.moe/user/api.php"
        data = {"reqtype": "fileupload"}
        with open(image_path, "rb") as f:
            res = requests.post(url, data=data, files={"fileToUpload": ("image.jpg", f, "image/jpeg")})
        res.raise_for_status()
        
        # Catbox returns the exact URL directly in plain text
        proxy_url = res.text.strip()
        logger.info(f"Successfully bridged image to CDNx: {proxy_url}")
        
    except Exception as e:
        logger.error(f"Proxy image upload failed: {e}")
        raise RuntimeError("Failed to proxy image via Telegraph CDN.") from e

    # 2. Call the regular PinResource create with the new public URL
    return upload_pin_from_url(session, board_id, proxy_url, title, description, link)
