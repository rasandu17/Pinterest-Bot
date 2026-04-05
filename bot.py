import os
import asyncio
import tempfile
import shutil
import uuid
from pathlib import Path
import logging

from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from downloader import download_instagram
from pinterest_uploader import get_session, upload_local_image_and_pin

# We can keep pending_uploads to handle multi-photo Instagram carousel posts,
# just like TumBot did.
pending_uploads: dict = {}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PINTEREST_BOARD_ID = os.getenv("PINTEREST_BOARD_ID", "").strip()

logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PinBot")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *PinBot* is ready!\n\n"
        "Send an *Instagram post/reel URL* to download and upload it to Pinterest.\n\n"
        "Make sure you have set `PINTEREST_BOARD_ID` in your environment!",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = (update.message.text or "").strip()

    if "instagram.com" not in url:
        await update.message.reply_text("⚠️ Please send a valid Instagram post or reel URL.")
        return

    if not PINTEREST_BOARD_ID:
        await update.message.reply_text("⚠️ The `PINTEREST_BOARD_ID` is missing in the environment.")
        return

    status_msg = await update.message.reply_text(
        f"⏳ Downloading from Instagram…\n`{url}`",
        parse_mode="Markdown",
    )

    tmp_dir = tempfile.mkdtemp(prefix="pinbot_")
    cleanup_needed = True

    try:
        # 1. Download the media
        media_path, media_type, caption = await asyncio.to_thread(
            download_instagram, url, tmp_dir
        )

        # 2. Check if it's a multi-photo carousel
        if isinstance(media_path, list) and len(media_path) > 1:
            cleanup_needed = False
            upload_id = str(uuid.uuid4())[:12]
            pending_uploads[upload_id] = {
                "media_path": media_path,
                "media_type": media_type,
                "caption": caption,
                "url": url,
                "tmp_dir": tmp_dir,
            }

            await status_msg.edit_text("🖼️ Sending previews, please wait…")
            preview_ids = []
            
            try:
                m_list = list(media_path)
                for i in range(0, len(m_list), 10):
                    batch = m_list[i:i+10]
                    offset = i
                    group = []
                    for j, p in enumerate(batch):
                        with open(str(p), "rb") as f:
                            if media_type == "video":
                                group.append(InputMediaVideo(f.read(), caption=f"Media {offset+j+1}"))
                            else:
                                group.append(InputMediaPhoto(f.read(), caption=f"Media {offset+j+1}"))
                    msgs = await update.message.reply_media_group(media=group)
                    preview_ids.extend([m.message_id for m in msgs])
            except Exception as e:
                logger.warning(f"Preview send failed: {e}")

            pending_uploads[upload_id]["preview_ids"] = preview_ids

            keyboard = _photo_select_keyboard(upload_id, len(media_path))
            await status_msg.edit_text(
                f"📸 This post has {len(media_path)} items.\n"
                "Pick which one to upload to Pinterest:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # 3. Single media item directly upload
        await status_msg.edit_text("✅ Downloaded! Uploading directly to Pinterest Board… 📍")
        pin_url = await _upload_to_pinterest_async(media_path, media_type, caption, url)
        
        await status_msg.edit_text(
            f"🎉 *Successfully pinned!*\n\n📍 Board: `{PINTEREST_BOARD_ID}`\n🔗 {pin_url}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.exception("Pipeline failed")
        await status_msg.edit_text(f"❌ Error: {exc}")
    finally:
        if cleanup_needed:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _upload_to_pinterest_async(path: str, media_type: str, title: str, source_link: str) -> str:
    """Wraps the reverse-engineered pinterest calls in a thread so they don't block Telegram"""
    
    def _do_upload():
        session = get_session()
        # Right now the reverse engineered logic works best for Photos
        if media_type == "video":
            # Pinterest undocumented video uploading is notoriously complex (it uses chunked AWS signed URLs).
            # If the ApiImageUploadResource doesn't accept mp4, we will instruct the user or use a workaround.
            # Let's try sending it to our current local endpoint and hope it parses as a standard file or fallback.
            # WARNING: This might fail on Pinterest's side if we don't supply video-specific properties.
            logger.info("Attempting to upload a video. Note: Reverse-engineered video upload on Pinterest can be restrictive.")
        
        resp = upload_local_image_and_pin(
            session=session,
            board_id=PINTEREST_BOARD_ID,
            image_path=path,
            title=title,
            description=title, # use caption as description
            link="https://sites.google.com/view/videowalt/home"
        )
        
        # Expecting something like 'https://pinterest.com/pin/1234567890/' back from the response data
        # resp['resource_response']['data']['url'] is typical
        try:
            data = resp.get('resource_response', {}).get('data', {})
            pin_url = data.get('url')
            
            if pin_url:
                if pin_url.startswith('/pin/'):
                    return "https://www.pinterest.com" + pin_url
                return pin_url
            
            # If no URL is found, we should just return a safe string instead of the raw dict, 
            # to prevent Telegram markdown parse crashes!
            logger.warning(f"Pin created, but URL missing in response: {str(resp)[:200]}")
            return "Check your Pinterest board (URL couldn't be parsed)!"
        except Exception as e:
            logger.error(f"Error extracting pin URL: {e}")
            return "Uploaded! (URL hidden)"

    return await asyncio.to_thread(_do_upload)

def _photo_select_keyboard(upload_id: str, count: int) -> list:
    keyboard = []
    row = []
    for i in range(count):
        row.append(InlineKeyboardButton(f"Media {i+1}", callback_data=f"up_{upload_id}_{i}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return keyboard


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if not data.startswith("up_"):
        return

    parts = data.split("_")
    upload_id = parts[1]
    selection = int(parts[2])

    if upload_id not in pending_uploads:
        await query.edit_message_text("❌ Session expired. Send the link again.")
        return

    session = pending_uploads.pop(upload_id)

    # Delete previews
    for mid in session.get("preview_ids", []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=mid)
        except:
            pass

    await query.edit_message_text("📤 Uploading selected item to Pinterest…")

    try:
        paths = session["media_path"]
        selected_path = list(paths)[selection]
        pin_url = await _upload_to_pinterest_async(
            selected_path, 
            str(session["media_type"]), 
            str(session["caption"]), 
            str(session["url"])
        )
        await query.edit_message_text(
            f"🎉 *Successfully pinned!*\n\n🔗 {pin_url}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("Upload callback failed")
        await query.edit_message_text(f"❌ Upload error: {exc}")
    finally:
        shutil.rmtree(str(session["tmp_dir"]), ignore_errors=True)

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment")
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("PinBot is running — waiting for messages…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
