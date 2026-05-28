import os
import re
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-]+"
)
SOUNDCLOUD_RE = re.compile(
    r"(https?://)?(www\.)?soundcloud\.com/[\w\-]+/[\w\-]+"
)
SPOTIFY_RE = re.compile(
    r"(https?://)?(open\.)?spotify\.com/(track|album|playlist)/[\w]+"
)


def detect_platform(url: str) -> str:
    if YOUTUBE_RE.search(url):
        return "youtube"
    if SOUNDCLOUD_RE.search(url):
        return "soundcloud"
    if SPOTIFY_RE.search(url):
        return "spotify"
    return "unknown"


async def get_spotify_search_query(url: str) -> str | None:
    """Extract track name from Spotify URL using yt-dlp metadata."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-download", "--print", "%(artist)s %(title)s", url],
            capture_output=True, text=True, timeout=30
        )
        query = result.stdout.strip()
        if query and query != "NA NA":
            return query
    except Exception:
        pass
    # Fallback: try spotdl for metadata
    try:
        result = subprocess.run(
            ["python3", "-m", "spotdl", "--print-errors", url],
            capture_output=True, text=True, timeout=30
        )
        # spotdl outputs "Artist - Title" format
        for line in result.stdout.splitlines():
            if " - " in line:
                return line.strip()
    except Exception:
        pass
    return None


async def download_audio(url: str, platform: str, tmpdir: str) -> tuple[str | None, str]:
    """Download audio and return (filepath, title)."""

    output_template = os.path.join(tmpdir, "%(title)s.%(ext)s")

    if platform == "spotify":
        # Get track info from Spotify, search on YouTube
        search_query = await get_spotify_search_query(url)
        if search_query:
            search_url = f"ytsearch1:{search_query}"
        else:
            return None, "Не удалось получить данные трека с Spotify"
        download_url = search_url
    else:
        download_url = url

    cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--add-metadata",
        "--no-playlist",
        "--max-filesize", "49M",
        "-o", output_template,
        download_url
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)

        if proc.returncode != 0:
            logger.error(f"yt-dlp error: {stderr.decode()}")
            return None, "Ошибка при скачивании. Проверь ссылку."

        # Find downloaded file
        mp3_files = list(Path(tmpdir).glob("*.mp3"))
        if not mp3_files:
            return None, "Файл не найден после скачивания."

        filepath = str(mp3_files[0])
        title = Path(filepath).stem
        return filepath, title

    except asyncio.TimeoutError:
        return None, "Таймаут: трек скачивается слишком долго (>3 мин)."
    except Exception as e:
        logger.exception(e)
        return None, f"Неизвестная ошибка: {e}"


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎵 *Музыкальный бот*\n\n"
        "Отправь мне ссылку на трек — и я пришлю MP3.\n\n"
        "Поддерживаю:\n"
        "• YouTube `youtube.com` / `youtu.be`\n"
        "• SoundCloud `soundcloud.com`\n"
        "• Spotify `open.spotify.com/track/...`\n\n"
        "Просто скинь ссылку 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text.strip()

    platform = detect_platform(text)

    if platform == "unknown":
        await message.reply_text(
            "❌ Не распознал ссылку.\n"
            "Поддерживаю: YouTube, SoundCloud, Spotify.\n\n"
            "Пример: `https://youtu.be/dQw4w9WgXcQ`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    icons = {"youtube": "▶️ YouTube", "soundcloud": "☁️ SoundCloud", "spotify": "🎧 Spotify"}
    status_msg = await message.reply_text(
        f"⏳ Скачиваю с {icons[platform]}...\nЭто займёт 10–60 секунд."
    )

    await ctx.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath, title = await download_audio(text, platform, tmpdir)

        if filepath is None:
            await status_msg.edit_text(f"❌ {title}")
            return

        file_size = os.path.getsize(filepath)
        if file_size > 50 * 1024 * 1024:
            await status_msg.edit_text("❌ Файл больше 50 МБ — Telegram не пропустит.")
            return

        await status_msg.edit_text("📤 Отправляю файл...")

        try:
            with open(filepath, "rb") as f:
                await message.reply_audio(
                    audio=f,
                    title=title,
                    caption=f"🎵 {title}\n\n_Скачано с {icons[platform]}_",
                    parse_mode=ParseMode.MARKDOWN
                )
            await status_msg.delete()
        except Exception as e:
            logger.exception(e)
            await status_msg.edit_text(f"❌ Не удалось отправить файл: {e}")


async def handle_other(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь мне ссылку на трек 🎵\n"
        "YouTube, SoundCloud или Spotify."
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан! Установи переменную окружения.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(~filters.TEXT, handle_other))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
