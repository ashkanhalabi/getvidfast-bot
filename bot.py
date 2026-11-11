#!/usr/bin/env python3
"""ربات تلگرام دانلود از یوتیوب و اینستاگرام.

- توکن فقط از متغیر محیطی BOT_TOKEN خوانده می‌شود (هرگز در کد یا فایل نوشته نمی‌شود).
- اولین نفری که /start بفرستد مالک ربات می‌شود (در data/owner.json ذخیره می‌شود)
  و ربات برای بقیه خصوصی است.
- رابط کاربری دوزبانه است (فارسی / English) با دکمه‌های درون‌خطی:
  منوی شروع (راهنما، زبان) و انتخاب فرمت (ویدیو / صوتی) بعد از فرستادن لینک.
- دانلود با yt-dlp انجام می‌شود؛ سقف حجم فایل ارسالی ۴۵ مگابایت است
  (محدودیت ربات تلگرام ۵۰ مگابایت است).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
# BOT_DATA_DIR lets a host keep owner/cookies across deploys
# (e.g. FadeHost: /data survives deploys, the code dir does not).
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", BASE_DIR / "data"))
OWNER_FILE = DATA_DIR / "owner.json"
LANG_FILE = DATA_DIR / "lang.json"
COOKIES_FILE = DATA_DIR / "cookies.txt"  # اختیاری؛ برای اینستاگرام

MAX_SEND_BYTES = 45 * 1024 * 1024  # سقف ۴۵ مگابایت برای ارسال با ربات تلگرام

YOUTUBE_FORMAT = (
    "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/"
    "bestvideo[height<=720]+bestaudio/"
    "best[height<=720]/best"
)
INSTAGRAM_FORMAT = "best/bestvideo+bestaudio"
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio"

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".wav"}
JUNK_SUFFIXES = (".part", ".ytdl", ".temp")

URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("tg-download-bot")

# فقط یک دانلود هم‌زمان، تا هاست کوچک رایگان زیر بار نرود.
download_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# مالک ربات
# ---------------------------------------------------------------------------

def get_owner_id():
    """شناسه مالک را از فایل می‌خواند؛ اگر هنوز کسی مالک نشده None برمی‌گرداند."""
    try:
        if OWNER_FILE.exists():
            return json.loads(OWNER_FILE.read_text(encoding="utf-8")).get("owner_id")
    except Exception as exc:  # noqa: BLE001 - خرابی فایل یعنی «مالک نداریم»
        log.warning("cannot read owner file: %s", exc)
    return None


def set_owner_id(user_id: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OWNER_FILE.write_text(
        json.dumps({"owner_id": user_id}, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# زبان رابط کاربری (فارسی / English)
# ---------------------------------------------------------------------------

def get_lang(user_id: int) -> str:
    """زبان انتخاب‌شده کاربر؛ پیش‌فرض فارسی."""
    try:
        if LANG_FILE.exists():
            lang = json.loads(LANG_FILE.read_text(encoding="utf-8")).get(str(user_id))
            if lang in STRINGS:
                return lang
    except Exception as exc:  # noqa: BLE001 - خرابی فایل یعنی «زبان پیش‌فرض»
        log.warning("cannot read lang file: %s", exc)
    return "fa"


def set_lang(user_id: int, lang: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    try:
        if LANG_FILE.exists():
            data = json.loads(LANG_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - فایل خراب را از اول می‌سازیم
        data = {}
    data[str(user_id)] = lang
    LANG_FILE.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )


STRINGS = {
    "fa": {
        "welcome": (
            "سلام! 👋\n\n"
            "من ربات دانلود هستم. لینک ویدیو از یوتیوب یا اینستاگرام بفرستید تا دانلودش کنم و همین‌جا تحویل بدهم.\n\n"
            "📌 یوتیوب: ویدیو با کیفیت حداکثر ۷۲۰p\n"
            "📌 اینستاگرام: پست، ریلز و ویدیو\n"
            "📌 اگر ویدیو از ۴۵ مگابایت بزرگ‌تر باشد، به‌جای آن فایل صوتی (mp3) را می‌فرستم.\n\n"
            "برای راهنما، دکمه زیر را بزنید."
        ),
        "help": (
            "راهنمای استفاده 📖\n\n"
            "۱. لینک ویدیو را از یوتیوب یا اینستاگرام بفرستید.\n"
            "   نمونه یوتیوب: youtube.com/watch?v=... یا youtu.be/...\n"
            "   نمونه اینستاگرام: instagram.com/reel/... یا instagram.com/p/...\n\n"
            "۲. فرمت را با دکمه انتخاب کنید: ویدیو یا صوتی (MP3).\n\n"
            "۳. صبر کنید تا دانلود و ارسال انجام شود.\n\n"
            "⚠️ محدودیت‌ها:\n"
            "• فایل ارسالی حداکثر ۴۵ مگابایت است (محدودیت تلگرام ۵۰ مگابایت است).\n"
            "• ویدیوهای خصوصی، حذف‌شده یا نیازمند ورود به حساب دانلود نمی‌شوند.\n"
            "• در هر لحظه فقط یک دانلود انجام می‌شود."
        ),
        "private_bot": "این ربات خصوصی است و فقط مالک آن می‌تواند از آن استفاده کند. 🙏",
        "need_start": "برای شروع، اول /start را بفرستید. 🙂",
        "busy": "در حال حاضر یک دانلود دیگر در حال انجام است؛ لطفاً کمی صبر کنید. ⏳",
        "not_a_link": (
            "لینک معتبر یوتیوب یا اینستاگرام پیدا نکردم. 🤔\n"
            "یک لینک ویدیو بفرستید یا برای راهنما /help را بزنید."
        ),
        "unknown": "دستور را نشناختم. 🤔 برای راهنما /help را بفرستید.",
        "choose_format": (
            "این لینک را با چه فرمتی بفرستم؟ 🎬🎵\n"
            "🎬 ویدیو (حداکثر ۷۲۰p)\n"
            "🎵 صوتی (MP3)"
        ),
        "downloading": "⏳ در حال دانلود…",
        "progress": "⏳ در حال دانلود… {pct}٪\n({dl} از {total} مگابایت)",
        "sending": "📤 در حال ارسال…",
        "too_large": (
            "این ویدیو حتی به‌صورت صوتی هم از ۴۵ مگابایت بزرگ‌تر است و "
            "نمی‌توانم آن را بفرستم. 📦"
        ),
        "audio_fallback": "ویدیو از ۴۵ مگابایت بزرگ‌تر بود، پس نسخه صوتی آن را می‌فرستم. 🎵",
        "cancelled": "لغو شد. ❌",
        "lang_prompt": "زبان ربات را انتخاب کنید: 🌐",
        "owner_claimed": "✅ شما به‌عنوان مالک ربات ثبت شدید.",
        "btn_help": "📖 راهنما",
        "btn_lang": "🌐 زبان",
        "btn_back": "🏠 منو",
        "btn_video": "🎬 ویدیو",
        "btn_audio": "🎵 صوتی (MP3)",
        "btn_cancel": "❌ لغو",
        "err_private": "این ویدیو خصوصی است و قابل دانلود نیست. 🔒",
        "err_unavailable": "این ویدیو در دسترس نیست؛ ممکن است حذف یا خصوصی شده باشد. 🗑️",
        "err_botcheck_ig": (
            "اینستاگرام برای این پست ورود به حساب می‌خواهد؛ "
            "بدون کوکی ورود نمی‌توان آن را دانلود کرد. 🔑"
        ),
        "err_botcheck_yt": (
            "یوتیوب این درخواست را به‌عنوان ربات تشخیص داده و ورود به حساب می‌خواهد. "
            "برای دانلود از یوتیوب باید کوکی حساب یوتیوب در ربات گذاشته شود. 🔑"
        ),
        "err_login_ig": (
            "اینستاگرام برای این پست ورود به حساب می‌خواهد؛ "
            "بدون کوکی ورود نمی‌توان آن را دانلود کرد. 🔑"
        ),
        "err_login_yt": (
            "یوتیوب برای این ویدیو ورود به حساب می‌خواهد؛ "
            "بدون کوکی ورود نمی‌توان آن را دانلود کرد. 🔑"
        ),
        "err_cookies": (
            "برای دانلود این لینک ورود به حساب لازم است؛ "
            "بدون کوکی ورود نمی‌توان آن را دانلود کرد. 🔑"
        ),
        "err_unsupported": "این لینک پشتیبانی نمی‌شود. ❌",
        "err_timeout": "اتصال به سرور طول کشید؛ لطفاً دوباره تلاش کنید. 🌐",
        "err_generic": "هنگام دانلود خطایی رخ داد؛ لطفاً دوباره تلاش کنید. ⚠️",
    },
    "en": {
        "welcome": (
            "Hello! 👋\n\n"
            "I'm a download bot. Send me a video link from YouTube or Instagram and I'll download it and send it right here.\n\n"
            "📌 YouTube: video up to 720p\n"
            "📌 Instagram: posts, reels and videos\n"
            "📌 If a video is larger than 45 MB, I'll send the audio file (mp3) instead.\n\n"
            "For the guide, tap the button below."
        ),
        "help": (
            "User guide 📖\n\n"
            "1. Send a video link from YouTube or Instagram.\n"
            "   YouTube example: youtube.com/watch?v=... or youtu.be/...\n"
            "   Instagram example: instagram.com/reel/... or instagram.com/p/...\n\n"
            "2. Pick a format with the buttons: video or audio (MP3).\n\n"
            "3. Wait for the download and sending to finish.\n\n"
            "⚠️ Limits:\n"
            "• Sent files are at most 45 MB (Telegram's limit is 50 MB).\n"
            "• Private, deleted or login-required videos cannot be downloaded.\n"
            "• Only one download runs at a time."
        ),
        "private_bot": "This bot is private and only its owner can use it. 🙏",
        "need_start": "To start, send /start first. 🙂",
        "busy": "Another download is in progress right now; please wait a bit. ⏳",
        "not_a_link": (
            "I couldn't find a valid YouTube or Instagram link. 🤔\n"
            "Send a video link or press /help for the guide."
        ),
        "unknown": "I didn't recognize that command. 🤔 Send /help for the guide.",
        "choose_format": (
            "Which format should I send this link in? 🎬🎵\n"
            "🎬 Video (up to 720p)\n"
            "🎵 Audio (MP3)"
        ),
        "downloading": "⏳ Downloading…",
        "progress": "⏳ Downloading… {pct}%\n({dl} of {total} MB)",
        "sending": "📤 Sending…",
        "too_large": (
            "This video is larger than 45 MB even as audio, "
            "so I can't send it. 📦"
        ),
        "audio_fallback": "The video was larger than 45 MB, so I'm sending its audio version instead. 🎵",
        "cancelled": "Cancelled. ❌",
        "lang_prompt": "Choose the bot language: 🌐",
        "owner_claimed": "✅ You are registered as the bot owner.",
        "btn_help": "📖 Help",
        "btn_lang": "🌐 Language",
        "btn_back": "🏠 Menu",
        "btn_video": "🎬 Video",
        "btn_audio": "🎵 Audio (MP3)",
        "btn_cancel": "❌ Cancel",
        "err_private": "This video is private and cannot be downloaded. 🔒",
        "err_unavailable": "This video is not available; it may have been deleted or made private. 🗑️",
        "err_botcheck_ig": (
            "Instagram wants a login for this post; "
            "without cookies it cannot be downloaded. 🔑"
        ),
        "err_botcheck_yt": (
            "YouTube flagged this request as a bot and wants a login. "
            "To download from YouTube, a YouTube account's cookies must be added to the bot. 🔑"
        ),
        "err_login_ig": (
            "Instagram wants a login for this post; "
            "without cookies it cannot be downloaded. 🔑"
        ),
        "err_login_yt": (
            "YouTube wants a login for this video; "
            "without cookies it cannot be downloaded. 🔑"
        ),
        "err_cookies": (
            "This link needs a login; "
            "without cookies it cannot be downloaded. 🔑"
        ),
        "err_unsupported": "This link is not supported. ❌",
        "err_timeout": "The connection took too long; please try again. 🌐",
        "err_generic": "An error happened while downloading; please try again. ⚠️",
    },
}


def t(lang: str, key: str) -> str:
    """متن کلید را به زبان کاربر برمی‌گرداند؛ فارسی پیش‌فرض است."""
    return STRINGS.get(lang, STRINGS["fa"])[key]


# ---------------------------------------------------------------------------
# دکمه‌های درون‌خطی
# ---------------------------------------------------------------------------

def main_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "btn_help"), callback_data="menu:help")],
            [InlineKeyboardButton(t(lang, "btn_lang"), callback_data="menu:lang")],
        ]
    )


def back_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu:main")]]
    )


def lang_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
                InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            ],
            [InlineKeyboardButton(t(lang, "btn_back"), callback_data="menu:main")],
        ]
    )


def format_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "btn_video"), callback_data="fmt:video"),
                InlineKeyboardButton(t(lang, "btn_audio"), callback_data="fmt:audio"),
            ],
            [InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="fmt:cancel")],
        ]
    )


# ---------------------------------------------------------------------------
# خطاها
# ---------------------------------------------------------------------------

class TooLargeError(Exception):
    """حتی نسخه صوتی هم از سقف حجم بزرگ‌تر است."""


def localize_error(exc: Exception, platform: str | None, lang: str) -> str:
    """پیام خطای yt-dlp را به یک پیام صادقانه به زبان کاربر تبدیل می‌کند.

    نکته: پیام انگلیسی یوتیوب برای ربات‌چک («Sign in to confirm you're not
    a bot... use --cookies...») کلمه cookies دارد؛ پس اول عبارت ربات‌چک
    بررسی می‌شود تا خطای یوتیوب اشتباه به‌عنوان خطای اینستاگرام نشان داده نشود.
    """
    msg = str(exc).lower()
    if "private" in msg:
        return t(lang, "err_private")
    if (
        "unavailable" in msg
        or "no longer available" in msg
        or "has been removed" in msg
        or "video unavailable" in msg
    ):
        return t(lang, "err_unavailable")
    if "confirm you're not a bot" in msg or "sign in to confirm" in msg:
        if platform == "instagram":
            return t(lang, "err_botcheck_ig")
        return t(lang, "err_botcheck_yt")
    if "login required" in msg or "log in" in msg:
        if platform == "instagram":
            return t(lang, "err_login_ig")
        return t(lang, "err_login_yt")
    if "cookies" in msg:
        return t(lang, "err_cookies")
    if "unsupported url" in msg:
        return t(lang, "err_unsupported")
    if "timed out" in msg or "timeout" in msg:
        return t(lang, "err_timeout")
    return t(lang, "err_generic")


# ---------------------------------------------------------------------------
# دانلود با yt-dlp (در یک ترد جدا اجرا می‌شود، چون همگام است)
# ---------------------------------------------------------------------------

def _base_opts(tmpdir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "writeinfojson": False,
        "writedescription": False,
        "writethumbnail": False,
        "merge_output_format": "mp4",
    }
    if COOKIES_FILE.exists():
        # اختیاری: اگر فایل data/cookies.txt وجود داشته باشد، برای
        # اینستاگرام (و یوتیوب محدودشده) استفاده می‌شود.
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def _estimated_size(info: dict) -> int | None:
    """حجم تقریبی فایل انتخاب‌شده را از اطلاعات yt-dlp می‌گیرد."""
    total = 0
    found = False
    for fmt in info.get("requested_formats") or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if size:
            total += size
            found = True
    if found:
        return total
    size = info.get("filesize") or info.get("filesize_approx")
    return size


class _Progress:
    def __init__(self):
        self.downloaded = 0
        self.total = None
        self.done = False

    def hook(self, d: dict):
        if d["status"] == "downloading":
            self.downloaded = d.get("downloaded_bytes") or 0
            self.total = d.get("total_bytes") or d.get("total_bytes_estimate")
        elif d["status"] == "finished":
            self.done = True


def _download(url: str, tmpdir: str, progress: _Progress, platform: str) -> dict:
    """دانلود را انجام می‌دهد و اطلاعات نتیجه را برمی‌گرداند.

    برمی‌گرداند: {"title": str, "files": [Path, ...], "as_audio": bool}
    اگر حتی نسخه صوتی هم بزرگ باشد TooLargeError می‌دهد.
    """
    video_format = YOUTUBE_FORMAT if platform == "youtube" else INSTAGRAM_FORMAT

    opts = _base_opts(tmpdir)
    opts["format"] = video_format
    opts["progress_hooks"] = [progress.hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title") or "video"
        est = _estimated_size(info)
        log.info("probe: title=%r platform=%s est_bytes=%s", title, platform, est)

        if est is not None and est > MAX_SEND_BYTES:
            log.info("video too large (%s bytes), falling back to audio", est)
            return _download_audio(ydl, url, tmpdir, title)

        ydl.download([url])

    files = _list_media_files(tmpdir)
    if not files:
        raise yt_dlp.utils.DownloadError("no media file produced")

    total_size = sum(f.stat().st_size for f in files)
    if total_size > MAX_SEND_BYTES:
        log.info("downloaded files too large (%s bytes), falling back to audio", total_size)
        for f in files:
            f.unlink(missing_ok=True)
        return _download_audio(None, url, tmpdir, title)

    return {"title": title, "files": files, "as_audio": False}


def _download_audio(ydl, url: str, tmpdir: str, title: str) -> dict:
    """نسخه صوتی (mp3) را دانلود می‌کند؛ اگر آن هم بزرگ بود خطا می‌دهد."""
    opts = _base_opts(tmpdir)
    opts["format"] = AUDIO_FORMAT
    opts["postprocessors"] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]
    with yt_dlp.YoutubeDL(opts) as audio_ydl:
        audio_ydl.download([url])

    files = _list_media_files(tmpdir)
    if not files:
        raise yt_dlp.utils.DownloadError("no audio file produced")
    total_size = sum(f.stat().st_size for f in files)
    if total_size > MAX_SEND_BYTES:
        for f in files:
            f.unlink(missing_ok=True)
        raise TooLargeError("audio too large")
    return {"title": title, "files": files, "as_audio": True}


def _download_audio_only(url: str, tmpdir: str) -> dict:
    """فقط نسخه صوتی را دانلود می‌کند (وقتی کاربر دکمه صوتی را زده است)."""
    with yt_dlp.YoutubeDL(_base_opts(tmpdir)) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title") or "audio"
    return _download_audio(None, url, tmpdir, title)


def _list_media_files(tmpdir: str) -> list[Path]:
    files = []
    for path in Path(tmpdir).iterdir():
        if not path.is_file():
            continue
        if path.name.endswith(JUNK_SUFFIXES):
            continue
        if path.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS:
            files.append(path)
    return sorted(files)


# ---------------------------------------------------------------------------
# هندلرهای تلگرام
# ---------------------------------------------------------------------------

def _is_owner(user_id: int) -> bool:
    owner = get_owner_id()
    return owner is not None and user_id == owner


def _detect_platform(url: str) -> str | None:
    if "instagram.com" in url:
        return "instagram"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    owner = get_owner_id()
    lang = get_lang(user.id)
    if owner is None:
        set_owner_id(user.id)
        log.info("owner claimed by user %s", user.id)
        await update.message.reply_text(
            t(lang, "welcome") + "\n\n" + t(lang, "owner_claimed"),
            reply_markup=main_menu_keyboard(lang),
        )
    elif user.id == owner:
        await update.message.reply_text(
            t(lang, "welcome"), reply_markup=main_menu_keyboard(lang)
        )
    else:
        await update.message.reply_text(t(lang, "private_bot"))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(
        t(lang, "help"), reply_markup=back_keyboard(lang)
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    lang = get_lang(user.id)

    owner = get_owner_id()
    if owner is None:
        await message.reply_text(t(lang, "need_start"))
        return
    if user.id != owner:
        await message.reply_text(t(lang, "private_bot"))
        return

    text = message.text or ""
    urls = URL_RE.findall(text)
    url = next((u for u in urls if _detect_platform(u)), None)
    if url is None:
        await message.reply_text(t(lang, "not_a_link"))
        return

    if download_lock.locked():
        await message.reply_text(t(lang, "busy"))
        return

    # لینک را نگه می‌داریم تا وقتی کاربر فرمت را با دکمه انتخاب کرد، دانلود شود.
    # (لینک در callback_data جا نمی‌شود؛ سقف آن ۶۴ بایت است.)
    context.user_data["pending_url"] = url
    await message.reply_text(
        t(lang, "choose_format"), reply_markup=format_keyboard(lang)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    lang = get_lang(user.id)

    owner = get_owner_id()
    if owner is None:
        await query.edit_message_text(t(lang, "need_start"))
        return
    if user.id != owner:
        await query.edit_message_text(t(lang, "private_bot"))
        return

    data = query.data or ""
    if data == "menu:help":
        await query.edit_message_text(
            t(lang, "help"), reply_markup=back_keyboard(lang)
        )
    elif data == "menu:lang":
        await query.edit_message_text(
            t(lang, "lang_prompt"), reply_markup=lang_keyboard(lang)
        )
    elif data == "menu:main":
        await query.edit_message_text(
            t(lang, "welcome"), reply_markup=main_menu_keyboard(lang)
        )
    elif data in ("lang:fa", "lang:en"):
        new_lang = data.split(":", 1)[1]
        set_lang(user.id, new_lang)
        await query.edit_message_text(
            t(new_lang, "welcome"),
            reply_markup=main_menu_keyboard(new_lang),
        )
    elif data in ("fmt:video", "fmt:audio", "fmt:cancel"):
        await _handle_format_choice(query, context, data, lang)
    else:
        await query.edit_message_text(t(lang, "unknown"))


async def _handle_format_choice(query, context, data: str, lang: str) -> None:
    if data == "fmt:cancel":
        context.user_data.pop("pending_url", None)
        await query.edit_message_text(t(lang, "cancelled"))
        return

    url = context.user_data.get("pending_url")
    if not url:
        await query.edit_message_text(t(lang, "not_a_link"))
        return
    context.user_data.pop("pending_url", None)

    platform = _detect_platform(url)
    if download_lock.locked():
        await query.edit_message_text(t(lang, "busy"))
        return

    mode = "video" if data == "fmt:video" else "audio"
    await _run_download(query, url, platform, mode, lang)


async def _run_download(query, url: str, platform: str, mode: str, lang: str) -> None:
    """دانلود انتخاب‌شده را انجام می‌دهد و نتیجه را می‌فرستد."""
    message = query.message
    async with download_lock:
        await query.edit_message_text(t(lang, "downloading"))
        tmpdir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            progress = _Progress()
            if mode == "audio":
                task = asyncio.create_task(
                    asyncio.to_thread(_download_audio_only, url, tmpdir)
                )
            else:
                task = asyncio.create_task(
                    asyncio.to_thread(_download, url, tmpdir, progress, platform)
                )
            last_text = ""
            while not task.done():
                await asyncio.sleep(2)
                if progress.total:
                    pct = int(progress.downloaded * 100 / progress.total)
                    text_now = t(lang, "progress").format(
                        pct=pct,
                        dl=progress.downloaded // (1024 * 1024),
                        total=progress.total // (1024 * 1024),
                    )
                    if text_now != last_text:
                        last_text = text_now
                        try:
                            await query.edit_message_text(text_now)
                        except Exception:  # noqa: BLE001 - ویرایش پیام مهم نیست
                            pass
            result = await task

            await query.edit_message_text(t(lang, "sending"))
            await _send_result(message, result, lang)
            try:
                await query.delete_message()
            except Exception:  # noqa: BLE001 - حذف پیام وضعیت مهم نیست
                pass
        except TooLargeError:
            await query.edit_message_text(t(lang, "too_large"))
        except yt_dlp.utils.DownloadError as exc:
            log.warning("download error for %s: %s", url, exc)
            await query.edit_message_text(localize_error(exc, platform, lang))
        except Exception as exc:  # noqa: BLE001 - هر خطای غیرمنتظره
            log.exception("unexpected error for %s", url)
            await query.edit_message_text(localize_error(exc, platform, lang))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


async def _send_result(message, result: dict, lang: str) -> None:
    """فایل‌های دانلودشده را به کاربر می‌فرستد."""
    files = result["files"]
    title = result["title"]

    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    audios = [f for f in files if f.suffix.lower() in AUDIO_EXTS]

    if result["as_audio"] and audios:
        await message.reply_text(t(lang, "audio_fallback"))

    if images and not videos and not audios:
        # پست عکس اینستاگرام (تکی یا چندتایی)
        from telegram import InputMediaPhoto

        media = [
            InputMediaPhoto(
                media=open(f, "rb"),
                caption=title if i == 0 else None,
            )
            for i, f in enumerate(images[:10])
        ]
        try:
            await message.reply_media_group(media=media)
        finally:
            for m in media:
                m.media.close()
        return

    for f in videos:
        with open(f, "rb") as fh:
            await message.reply_video(
                video=fh, caption=title, supports_streaming=True
            )
    for f in audios:
        with open(f, "rb") as fh:
            await message.reply_audio(audio=fh, caption=title, title=title)
    for f in images:
        with open(f, "rb") as fh:
            await message.reply_photo(photo=fh, caption=title)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "unknown"))


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.error("BOT_TOKEN environment variable is not set; exiting.")
        raise SystemExit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if COOKIES_FILE.exists():
        log.info("cookies file found; it will be used for downloads.")
    else:
        log.info("no cookies file; Instagram downloads may be limited.")

    # Timeout های پیش‌فرض (۵ ثانیه) برای آپلود فایل‌های بزرگ و شبکه‌های کند
    # کم است؛ پس زمان‌بندی بازتری می‌گذاریم.
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(token).request(request).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    log.info("bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
