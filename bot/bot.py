"""
Veilnova Fetch Node — Telegram bot for downloading media by URL and sending it back to chat.
Veilnova Fetch Node — Telegram-бот для скачивания медиа по ссылке и отправки обратно в чат.

Основная идея / High-level:
- Бот принимает ссылку, определяет стратегию скачивания (yt-dlp / gallery-dl),
  скачивает файлы во временную директорию и отправляет их пользователю.
- The bot accepts a URL, chooses a download strategy (yt-dlp / gallery-dl),
  downloads files into a temporary directory and sends them back to the user.

Особенности / Key points:
- Поддерживается локальный Bot API сервер на TDLib (telegram-bot-api контейнер).
  Это позволяет отправлять файлы большего размера (в рамках практических лимитов).
- Supports local TDLib-based Bot API server (telegram-bot-api container),
  which is useful for larger file uploads (within practical limits).

- Для TikTok используется актуальная версия yt-dlp из master + impersonation (curl-cffi),
  т.к. TikTok часто ломает extractor в stable релизах.
- TikTok is handled via yt-dlp master + impersonation (curl-cffi),
  because TikTok frequently breaks the extractor in stable releases.

Безопасность / Security:
- Не храните BOT_TOKEN/TDLib креды в репозитории.
- Do not commit BOT_TOKEN / TDLib credentials to the repository.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import CommandStart
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Env helpers (simple, tolerant parsing).
# Хелперы для переменных окружения (простое и устойчивое парсирование).


def env_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name, "").strip()
    return v if v else default


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def setup_logging(level: str, log_file: str) -> logging.Logger:
    logger = logging.getLogger("bot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    return logger


# Run external CLI command and capture stdout/stderr.
# Запуск внешней команды (CLI) с захватом stdout/stderr.
#
# NOTE: We intentionally decode with errors="ignore" to avoid crashes on weird byte output.
# NOTE: Специально декодируем с errors="ignore", чтобы не падать на “битых” байтах вывода.


async def run_cmd(args: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    out = (stdout_b or b"").decode("utf-8", errors="ignore")
    err = (stderr_b or b"").decode("utf-8", errors="ignore")
    return proc.returncode or 0, out, err


def load_first_json(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("Empty yt-dlp output")

    first = s.splitlines()[0].strip()
    if first.startswith("{") and first.endswith("}"):
        return json.loads(first)

    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in yt-dlp output")
    return json.loads(s[start : end + 1])


def build_caption(title: str, description: str, max_len: int = 1024) -> str:
    desc = (description or "").strip()
    ttl = (title or "").strip()
    text = (desc or ttl).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def is_instagram(url: str) -> bool:
    u = url.lower()
    return "instagram.com" in u or "instagr.am" in u


def is_instagram_post(url: str) -> bool:
    u = url.lower()
    return "instagram.com/p/" in u


def is_pinterest(url: str) -> bool:
    u = url.lower()
    return "pinterest." in u or "pin.it" in u


def is_tiktok(url: str) -> bool:
    u = url.lower()
    return "tiktok.com" in u or "vt.tiktok.com" in u


def is_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def supports_quality_menu(url: str) -> bool:
    """
    Платформы, где имеет смысл давать выбор качества/аудио.
    Instagram/TikTok/Pinterest исключаем явно.
    """
    if is_instagram(url) or is_tiktok(url) or is_pinterest(url):
        return False
    u = url.lower()
    hosts = [
        "youtube.com",
        "youtu.be",
        "vk.com",
        "vkvideo.ru",
        "rutube.ru",
        "vimeo.com",
        "dailymotion.com",
        "twitch.tv",
    ]
    return any(h in u for h in hosts)


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def guess_send_method(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".mp4", ".mkv", ".webm", ".mov"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext in {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}:
        return "audio"
    return "document"


def extract_estimated_size_bytes(info: Dict[str, Any]) -> Optional[int]:
    for key in ("filesize", "filesize_approx"):
        v = info.get(key)
        if isinstance(v, int) and v > 0:
            return v

    rd = info.get("requested_downloads")
    if isinstance(rd, list):
        for item in rd:
            if not isinstance(item, dict):
                continue
            for key in ("filesize", "filesize_approx"):
                v = item.get(key)
                if isinstance(v, int) and v > 0:
                    return v
    return None


def file_uri(abs_path: Path) -> str:
    p = abs_path.resolve()
    return f"file://{p.as_posix()}"


def resolve_cookies_source() -> Optional[Path]:
    src = os.getenv("YTDLP_COOKIES", "").strip()
    if src:
        p = Path(src)
        return p if p.exists() else None

    # We keep cookies as a Docker-mounted secret (read-only) and optionally copy it into a writable path.
    # Cookies часто монтируются в контейнер как read-only secret; при необходимости копируем в writable-файл.
    #
    # Why / Зачем:
    # - Some downloaders/tools may try to update cookies or require writable file handles.
    # - Некоторые инструменты пытаются обновлять cookies или требуют запись в файл.

    fallback = Path("/run/secrets/cookies.txt")
    return fallback if fallback.exists() else None


def prepare_writable_cookies(work_dir: Path) -> Optional[Path]:
    src_path = resolve_cookies_source()
    if not src_path:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    dst = work_dir / "instagram.cookies.txt"
    shutil.copyfile(src_path, dst)
    return dst


def yt_dlp_common_args(url: str, cookies_path: Optional[Path]) -> List[str]:
    args: List[str] = [
        *([] if is_instagram_post(url) else ["--no-playlist"]),
        "--restrict-filenames",
        "--no-progress",
    ]

        # --- YouTube hardening (EJS + avoid web_safari SABR 403) ---
    if is_youtube(url):
        # Ensure EJS scripts are available (auto-download)
        # See yt-dlp wiki: EJS / remote-components
        args += ["--remote-components", YTDLP_REMOTE_COMPONENTS]

        # Force player clients that are less likely to trigger SABR/web_safari missing URL -> 403
        args += ["--extractor-args", f"youtube:player_client={YTDLP_YOUTUBE_PLAYER_CLIENT}"]


    # --- TikTok hardening ---
    if is_tiktok(url):
        # TLS/browser fingerprint (curl_cffi)
        args += ["--impersonate", YTDLP_IMPERSONATE]

        # Часто помогает против "другой" HTML-выдачи/AB вариантов
        args += ["--add-header", "Referer:https://www.tiktok.com/"]
        args += ["--add-header", "Accept-Language:en-US,en;q=0.9"]

        # Явный UA (иногда TikTok отдаёт "урезанную" страницу без данных)
        args += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        ]

        if YTDLP_FORCE_IPV4:
            args += ["-4"]

    if cookies_path:
        args += ["--cookies", str(cookies_path)]

    if is_instagram(url):
        args += [
            "--sleep-requests",
            "2",
            "--sleep-interval",
            "2",
            "--max-sleep-interval",
            "4",
        ]
        args += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        ]

    if is_instagram_post(url):
        args += ["--ignore-no-formats-error", "--no-abort-on-error"]

    return args


def extract_available_heights(info: Dict[str, Any]) -> List[int]:
    heights: set[int] = set()
    fmts = info.get("formats")
    if isinstance(fmts, list):
        for f in fmts:
            if not isinstance(f, dict):
                continue
            h = f.get("height")
            vcodec = f.get("vcodec")
            if vcodec in (None, "none"):
                continue
            if isinstance(h, int) and h > 0:
                heights.add(h)
    return sorted(heights, reverse=True)


def pick_menu_heights(available: List[int]) -> List[int]:
    wanted = [1080, 720, 480, 360]
    avail_set = set(available)
    return [h for h in wanted if h in avail_set]


def fmt_selector_for_height(height: int) -> str:
    return f"bv*[height<={height}]+ba/b[height<={height}]/best[height<={height}]"


def build_quality_keyboard(token: str, heights: List[int]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []

    # Верхний ряд: Best / MP3
    rows.append(
        [
            InlineKeyboardButton(text="🔥 Лучшее", callback_data=f"q|best|{token}"),
            InlineKeyboardButton(text="🎵 MP3", callback_data=f"q|mp3|{token}"),
        ]
    )

    quality_labels = {
        1080: "🎬 1080p",
        720: "📺 720p",
        480: "📱 480p",
        360: "⚡ 360p",
    }

    # Кнопки качества (2 в ряд)
    btns: List[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=quality_labels.get(h, f"{h}p"),
            callback_data=f"q|{h}p|{token}",
        )
        for h in heights
    ]

    for i in range(0, len(btns), 2):
        rows.append(btns[i : i + 2])

    # Нижний ряд: Отмена (на всю ширину)
    rows.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"q|cancel|{token}")]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


@dataclass
class DownloadPlan:
    mode: str  # "video" | "photo" | "gallery"
    title: str = ""
    description: str = ""
    info: Optional[Dict[str, Any]] = None


@dataclass
class PendingJob:
    token: str
    chat_id: int
    user_id: int
    url: str
    title: str
    description: str
    created_at: float
    expires_at: float
    status_message_id: int


async def safe_edit_text(
    msg: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """
    Telegram ругается, если пытаемся изменить сообщение на идентичное ("message is not modified").
    Это не ошибка бизнес-логики, поэтому гасим только этот кейс.
    """
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        s = str(e)
        if "message is not modified" in s:
            return
        raise


async def precheck_plan(
    url: str, max_file_mb: int, download_dir: Path, logger: logging.Logger
) -> DownloadPlan:
    # Instagram /p/ -> photo mode, там карусели/фото, yt-dlp precheck не нужен
    if is_instagram_post(url):
        logger.info("PRECHECK | instagram post (/p/) -> photo-mode")
        return DownloadPlan(mode="photo", title="", description="", info=None)

    # Pinterest и TikTok: устойчивее через gallery-dl, precheck yt-dlp не нужен
    if is_pinterest(url):
        logger.info("PRECHECK | pinterest -> gallery-mode (skip yt-dlp precheck)")
        return DownloadPlan(mode="gallery", title="", description="", info=None)

    if is_tiktok(url):
        logger.info("PRECHECK | tiktok -> video-mode (yt-dlp + impersonate)")
        # TikTok: intentionally skip precheck.
        # TikTok: намеренно пропускаем precheck.
        #
        # RU: TikTok часто меняет выдачу/HTML, а precheck (--dump-single-json --skip-download)
        #     может падать или давать неполные данные. Надёжнее сразу выполнять скачивание.
        # EN: TikTok frequently changes responses/HTML; precheck (--dump-single-json --skip-download)
        #     may fail or produce incomplete data. It is more reliable to go straight to download.
        return DownloadPlan(mode="video", title="", description="", info=None)

    cookies_copy = prepare_writable_cookies(download_dir)

    args = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--quiet",
        "--no-warnings",
        *yt_dlp_common_args(url, cookies_copy),
        url,
    ]

    rc, out, err = await run_cmd(args)
    if rc != 0:
        msg = (err or out).strip()
        if len(msg) > 1200:
            msg = msg[-1200:]
        raise RuntimeError(f"yt-dlp precheck error (code {rc}):\n{msg}")

    info = load_first_json(out)
    title = str(info.get("title") or "").strip()
    description = str(info.get("description") or "").strip()

    size_bytes = extract_estimated_size_bytes(info)
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        if size_mb > max_file_mb:
            raise RuntimeError(
                f"Файл ~{size_mb:.1f} MB превышает лимит {max_file_mb} MB. Скачивание отменено."
            )

    return DownloadPlan(mode="video", title=title, description=description, info=info)


async def run_gallery_dl_download(
    url: str, out_dir: Path, logger: logging.Logger
) -> List[Path]:
    job_id = uuid.uuid4().hex
    job_dir = out_dir / f"gdl_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    cookies_copy = prepare_writable_cookies(job_dir)
    args = ["gallery-dl", "-d", str(job_dir)]
    if cookies_copy:
        args += ["--cookies", str(cookies_copy)]
    args += [url]

    logger.info("GALLERY_DL | start | dir=%s", str(job_dir))

    rc, out, err = await run_cmd(args)
    if rc != 0:
        msg = (err or out).strip()
        if len(msg) > 1200:
            msg = msg[-1200:]
        raise RuntimeError(f"gallery-dl error (code {rc}):\n{msg}")

    files = [p for p in job_dir.rglob("*") if p.is_file()]
    files = [
        p
        for p in files
        if p.name.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mkv")
        )
    ]
    files.sort(key=lambda p: p.name.lower())
    if not files:
        raise RuntimeError("gallery-dl finished, but no files were found.")
    logger.info("GALLERY_DL | done | files=%d", len(files))
    return files


async def run_yt_dlp_download(
    url: str,
    out_dir: Path,
    max_file_mb: int,
    mode: str,
    logger: logging.Logger,
    quality_choice: str = "best",
) -> List[Path]:
    job_id = uuid.uuid4().hex
    outtmpl = str(out_dir / f"{job_id}.%(autonumber)03d.%(title).200s.%(ext)s")

    cookies_copy = prepare_writable_cookies(out_dir)
    common = yt_dlp_common_args(url, cookies_copy)

    if is_tiktok(url):
        try:
            v_rc, v_out, v_err = await run_cmd(["yt-dlp", "--version"])
            logger.info("TIKTOK | yt-dlp version=%s", (v_out or v_err).strip())
            logger.info(
                "TIKTOK | impersonate=%s ipv4=%s", YTDLP_IMPERSONATE, YTDLP_FORCE_IPV4
            )
        except Exception:
            logger.exception("TIKTOK | failed to read yt-dlp version")

    # MP3 — отдельный режим
    if quality_choice == "mp3":
        args = [
            "yt-dlp",
            *common,
            "-f",
            "bestaudio/best",
            "--max-filesize",
            f"{max_file_mb}M",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "-o",
            outtmpl,
            url,
        ]
        logger.info("YTDLP | start | mode=%s quality=%s", mode, quality_choice)
        rc, out, err = await run_cmd(args)
        if rc != 0:
            msg = (err or out).strip()
            if len(msg) > 1200:
                msg = msg[-1200:]
            raise RuntimeError(f"yt-dlp download error (code {rc}):\n{msg}")

        candidates = sorted(out_dir.glob(f"{job_id}.*"))
        if not candidates:
            candidates = sorted(out_dir.rglob(f"{job_id}.*"))
        if not candidates:
            raise RuntimeError("Download finished, but output file was not found.")
        candidates = [p for p in candidates if p.is_file()]
        candidates.sort(key=lambda p: p.name.lower())
        logger.info("YTDLP | done | files=%d", len(candidates))
        return candidates

    # video
    if mode == "video":
        # ВАЖНО: для "best" не передаём "-f best", чтобы yt-dlp скачал и смёржил лучший набор форматов
        if quality_choice == "best":
            fmt = None
        elif quality_choice.endswith("p") and quality_choice[:-1].isdigit():
            h = int(quality_choice[:-1])
            fmt = fmt_selector_for_height(h)
        else:
            fmt = None

        args = [
            "yt-dlp",
            *common,
            *(["-f", fmt] if fmt else []),
            "--max-filesize",
            f"{max_file_mb}M",
            "--merge-output-format",
            "mp4",
            "-o",
            outtmpl,
            url,
        ]
    else:
        args = [
            "yt-dlp",
            *common,
            "--max-filesize",
            f"{max_file_mb}M",
            "-o",
            outtmpl,
            url,
        ]

    logger.info("YTDLP | start | mode=%s quality=%s", mode, quality_choice)

    rc, out, err = await run_cmd(args)
    if rc != 0:
        msg = (err or out).strip()

        # Fallback to gallery-dl for Pinterest / Instagram posts if yt-dlp returned no usable formats.
        # Fallback на gallery-dl для Pinterest / Instagram постов, если yt-dlp не вернул форматов.
        #
        # RU: Иногда сайты “режут” форматы для yt-dlp, но gallery-dl умеет доставать медиа из поста.
        # EN: Sometimes a site returns no formats for yt-dlp, while gallery-dl can still extract post media.

        if ("No video formats found" in msg) and (
            is_instagram_post(url) or is_pinterest(url)
        ):
            logger.warning(
                "YTDLP | no formats (%s) -> fallback to gallery-dl",
                "pinterest" if is_pinterest(url) else "instagram",
            )
            return await run_gallery_dl_download(url, out_dir, logger)

        if len(msg) > 1200:
            msg = msg[-1200:]
        raise RuntimeError(f"yt-dlp download error (code {rc}):\n{msg}")

    candidates = sorted(out_dir.glob(f"{job_id}.*"))
    if not candidates:
        candidates = sorted(out_dir.rglob(f"{job_id}.*"))
    if not candidates:
        raise RuntimeError("Download finished, but output file was not found.")

    candidates = [p for p in candidates if p.is_file()]
    candidates.sort(key=lambda p: p.name.lower())
    logger.info("YTDLP | done | files=%d", len(candidates))
    return candidates


async def send_media_group_photos(
    bot: Bot, chat_id: int, photos: List[Path], caption: str, logger: logging.Logger
) -> None:
    for idx in range(0, len(photos), 10):
        batch = photos[idx : idx + 10]
        media: List[InputMediaPhoto] = []
        for j, p in enumerate(batch):
            cap = caption if (idx == 0 and j == 0 and caption) else None
            media.append(InputMediaPhoto(media=FSInputFile(str(p)), caption=cap))
        logger.info("SEND_MEDIA_GROUP_PHOTO | %d..%d", idx + 1, idx + len(batch))
        await bot.send_media_group(chat_id=chat_id, media=media)


async def send_media_group_videos(
    bot: Bot, chat_id: int, videos: List[Path], caption: str, logger: logging.Logger
) -> None:
    for idx in range(0, len(videos), 10):
        batch = videos[idx : idx + 10]
        media: List[InputMediaVideo] = []
        for j, p in enumerate(batch):
            cap = caption if (idx == 0 and j == 0 and caption) else None
            media.append(InputMediaVideo(media=FSInputFile(str(p)), caption=cap))
        logger.info("SEND_MEDIA_GROUP_VIDEO | %d..%d", idx + 1, idx + len(batch))
        await bot.send_media_group(chat_id=chat_id, media=media)


async def safe_remove(path: Path, logger: logging.Logger) -> None:
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        logger.exception("CLEANUP_FAIL | path=%s", str(path))


async def send_video_safe(
    bot: Bot, chat_id: int, p: Path, caption: str, logger: logging.Logger
) -> None:
    try:
        await bot.send_video(
            chat_id=chat_id, video=file_uri(p), caption=caption or None
        )
    except Exception as e:
        msg = str(e)
        if "Unsupported URL protocol" in msg or "invalid file HTTP URL" in msg:
            logger.warning("SEND_VIDEO | local failed -> upload fallback | %s", msg)
            await bot.send_video(
                chat_id=chat_id, video=FSInputFile(str(p)), caption=caption or None
            )
        else:
            raise


async def send_audio_safe(
    bot: Bot, chat_id: int, p: Path, caption: str, logger: logging.Logger
) -> None:
    try:
        await bot.send_audio(
            chat_id=chat_id, audio=file_uri(p), caption=caption or None
        )
    except Exception as e:
        msg = str(e)
        if "Unsupported URL protocol" in msg or "invalid file HTTP URL" in msg:
            logger.warning("SEND_AUDIO | local failed -> upload fallback | %s", msg)
            await bot.send_audio(
                chat_id=chat_id, audio=FSInputFile(str(p)), caption=caption or None
            )
        else:
            raise


async def send_document_safe(
    bot: Bot, chat_id: int, p: Path, caption: str, logger: logging.Logger
) -> None:
    try:
        await bot.send_document(
            chat_id=chat_id, document=file_uri(p), caption=caption or None
        )
    except Exception as e:
        msg = str(e)
        if "Unsupported URL protocol" in msg or "invalid file HTTP URL" in msg:
            logger.warning("SEND_DOC | local failed -> upload fallback | %s", msg)
            await bot.send_document(
                chat_id=chat_id, document=FSInputFile(str(p)), caption=caption or None
            )
        else:
            raise


def is_timeout_error(e: Exception) -> bool:
    s = str(e).lower()
    return (
        "timeout" in s or "request timeout" in s or isinstance(e, asyncio.TimeoutError)
    )


def purge_expired_jobs() -> None:
    now = time.time()
    expired = [k for k, v in pending_jobs.items() if v.expires_at <= now]
    for k in expired:
        pending_jobs.pop(k, None)


async def process_download_and_send(
    bot: Bot,
    chat_id: int,
    url: str,
    status: Message,
    plan: DownloadPlan,
    quality_choice: str,
) -> None:
    downloaded: List[Path] = []
    caption = build_caption(plan.title, plan.description)

    # Скачивание
    await safe_edit_text(status, "🔄 В процессе…")

    if plan.mode == "gallery":
        downloaded = await run_gallery_dl_download(url, DOWNLOAD_DIR, logger)
    else:
        downloaded = await run_yt_dlp_download(
            url,
            DOWNLOAD_DIR,
            MAX_FILE_MB,
            plan.mode,
            logger,
            quality_choice=quality_choice,
        )

    downloaded = [p.resolve() for p in downloaded if p.exists() and p.is_file()]

    # лимит на отправку
    for p in downloaded:
        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb > TG_MAX_UPLOAD_MB:
            raise RuntimeError(
                f"Файл {p.name} ({size_mb:.1f} MB) превышает лимит {TG_MAX_UPLOAD_MB} MB."
            )

    images = [p for p in downloaded if is_image_file(p)]
    others = [p for p in downloaded if p not in images]
    videos = [p for p in others if guess_send_method(p) == "video"]
    audios = [p for p in others if guess_send_method(p) == "audio"]
    non_media_docs = [p for p in others if p not in videos and p not in audios]

    if images:
        await safe_edit_text(status, f"📸 Подготовлено фото: {len(images)}. Отправляю…")
        await send_media_group_photos(bot, chat_id, images, caption, logger)

    if videos:
        if len(videos) > 1:
            await safe_edit_text(
                status, f"🎬 Подготовлено видео: {len(videos)}. Отправляю…"
            )
            await send_media_group_videos(bot, chat_id, videos, caption, logger)
        else:
            await safe_edit_text(status, "🎥 Отправляю видео…")
            await send_video_safe(bot, chat_id, videos[0], caption, logger)

    if audios:
        for i, p in enumerate(audios):
            await safe_edit_text(status, f"🎧 Отправляю аудио ({i+1}/{len(audios)})…")
            await send_audio_safe(bot, chat_id, p, caption if i == 0 else "", logger)

    for p in non_media_docs:
        await safe_edit_text(status, f"📤 Отправляю файл: {p.name}")
        await send_document_safe(bot, chat_id, p, caption, logger)

    # Финальный статус (опционально удаляем)
    await safe_edit_text(status, "✅ Готово.")

    logger.info(
        "DONE | chat=%s url=%s files=%d quality=%s",
        chat_id,
        url,
        len(downloaded),
        quality_choice,
    )

    if DELETE_STATUS_ON_SUCCESS:
        try:
            await asyncio.sleep(max(0, DELETE_STATUS_DELAY_SEC))
            await status.delete()
        except Exception:
            pass

    # cleanup
    for p in downloaded:
        # gallery-dl складывает в gdl_*/..., удаляем папку целиком
        if "gdl_" in str(p):
            await safe_remove(p.parent, logger)
        else:
            await safe_remove(p, logger)


load_dotenv()

BOT_TOKEN = env_str("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing")

DOWNLOAD_DIR = Path(env_str("DOWNLOAD_DIR", "/data")).resolve()
MAX_FILE_MB = env_int("MAX_FILE_MB", 1900)
TG_MAX_UPLOAD_MB = env_int("TG_MAX_UPLOAD_MB", 1900)

LOG_LEVEL = env_str("LOG_LEVEL", "INFO")
LOG_FILE = env_str("LOG_FILE", "/app/bot.log")
BOT_API_BASE_URL = env_str("BOT_API_BASE_URL", "").rstrip("/")

BOT_SESSION_TIMEOUT = env_int("BOT_SESSION_TIMEOUT", 600)
QUALITY_MENU_TTL_SEC = env_int("QUALITY_MENU_TTL_SEC", 600)

DELETE_STATUS_ON_SUCCESS = env_bool("DELETE_STATUS_ON_SUCCESS", True)
DELETE_STATUS_DELAY_SEC = env_int("DELETE_STATUS_DELAY_SEC", 2)

YTDLP_IMPERSONATE = env_str("YTDLP_IMPERSONATE", "chrome")
YTDLP_FORCE_IPV4 = env_bool("YTDLP_FORCE_IPV4", True)

YTDLP_YOUTUBE_PLAYER_CLIENT = env_str("YTDLP_YOUTUBE_PLAYER_CLIENT", "web_embedded,web,tv")
YTDLP_REMOTE_COMPONENTS = env_str("YTDLP_REMOTE_COMPONENTS", "ejs:github")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logger = setup_logging(LOG_LEVEL, LOG_FILE)
dp = Dispatcher()
chat_locks: Dict[int, asyncio.Lock] = {}
pending_jobs: Dict[str, PendingJob] = {}


def get_lock(chat_id: int) -> asyncio.Lock:
    lock = chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        chat_locks[chat_id] = lock
    return lock


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Привет.\n\n"
        "Я помогу скачать медиа по ссылке и отправлю файлы прямо в этот чат.\n\n"
        "Поддерживаются популярные платформы:\n"
        "• Instagram\n"
        "• TikTok\n"
        "• YouTube\n"
        "• Pinterest\n"
        "• и другие открытые источники\n\n"
        "Как это работает:\n"
        "— ты отправляешь ссылку\n"
        "— при необходимости выбираешь качество\n"
        "— я загружаю и отправляю файлы\n\n"
        "Ничего лишнего: без регистрации, без водяных знаков, без внешних сайтов.\n\n"
        "Просто пришли ссылку — я начну 📥"
    )


@dp.callback_query(F.data.startswith("q|"))
async def on_quality_choice(call: CallbackQuery, bot: Bot) -> None:
    purge_expired_jobs()

    data = (call.data or "").split("|", 2)
    if len(data) != 3:
        await call.answer("⚠️ Некорректный выбор. Попробуй ещё раз.", show_alert=True)
        return

    _, choice, token = data
    job = pending_jobs.get(token)
    if not job:
        await call.answer("⌛ Запрос устарел. Пришли ссылку заново.", show_alert=True)
        return

    if not call.from_user or call.from_user.id != job.user_id:
        await call.answer(
            "ℹ️ Это меню не относится к текущему запросу.", show_alert=True
        )
        return

    if choice == "cancel":
        pending_jobs.pop(token, None)
        await call.answer("🚫 Отменено.")
        try:
            await safe_edit_text(
                call.message, "🚫 Запрос отменён. Пришли ссылку заново."
            )
        except Exception:
            pass
        return

    pending_jobs.pop(token, None)
    await call.answer("🔄 В процессе…")

    lock = get_lock(job.chat_id)
    if lock.locked():
        await call.answer("⏳ Загрузка ещё идёт. Пожалуйста, подожди.", show_alert=True)
        return

    async with lock:
        status_msg = call.message
        if not status_msg:
            return

        try:
            plan = DownloadPlan(
                mode="video", title=job.title, description=job.description, info=None
            )
            await process_download_and_send(
                bot, job.chat_id, job.url, status_msg, plan, quality_choice=choice
            )

        except Exception as e:
            if is_timeout_error(e):
                logger.warning(
                    "TIMEOUT | chat=%s url=%s err=%s", job.chat_id, job.url, str(e)
                )
                try:
                    await safe_edit_text(
                        status_msg,
                        "⏱ Возможна задержка со стороны Telegram.\nЕсли файл уже появился — всё в порядке.\nЕсли нет, попробуй повторить запрос чуть позже.",
                    )
                except Exception:
                    pass
            else:
                logger.exception("FAIL | chat=%s url=%s", job.chat_id, job.url)
                try:
                    await safe_edit_text(
                        status_msg, f"❌ Не удалось выполнить запрос:\n{e}"
                    )
                except Exception:
                    pass


@dp.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    text = (message.text or "").strip()
    m = URL_RE.search(text)
    if not m:
        return

    url = m.group(0)

    lock = get_lock(message.chat.id)
    if lock.locked():
        await message.reply("⏳ Загрузка ещё идёт. Пожалуйста, подожди.")
        return

    async with lock:
        status = await message.reply("🔄 В процессе…")

        try:
            logger.info(
                "REQUEST | chat=%s user=%s url=%s",
                message.chat.id,
                message.from_user.id if message.from_user else "-",
                url,
            )

            plan = await precheck_plan(url, MAX_FILE_MB, DOWNLOAD_DIR, logger)
            logger.info(
                "PLAN | mode=%s title=%s cookies=%s",
                plan.mode,
                plan.title or "-",
                str(resolve_cookies_source() or "-"),
            )

            # Меню качества — только video + поддерживаемые платформы + есть info
            if plan.mode == "video" and supports_quality_menu(url) and plan.info:
                avail_heights = extract_available_heights(plan.info)
                menu_heights = pick_menu_heights(avail_heights)

                token = uuid.uuid4().hex[:12]
                now = time.time()
                pending_jobs[token] = PendingJob(
                    token=token,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id if message.from_user else 0,
                    url=url,
                    title=plan.title,
                    description=plan.description,
                    created_at=now,
                    expires_at=now + QUALITY_MENU_TTL_SEC,
                    status_message_id=status.message_id,
                )

                kb = build_quality_keyboard(token, menu_heights)
                await safe_edit_text(
                    status, "🎚 Выбери нужное качество:", reply_markup=kb
                )
                return

            # Без меню
            await process_download_and_send(
                bot, message.chat.id, url, status, plan, quality_choice="best"
            )

        except Exception as e:
            if is_timeout_error(e):
                logger.warning(
                    "TIMEOUT | chat=%s url=%s err=%s", message.chat.id, url, str(e)
                )
                await safe_edit_text(
                    status,
                    "⏱ Возможна задержка со стороны Telegram.\nЕсли файл уже появился — всё в порядке.\nЕсли нет, попробуй повторить запрос чуть позже.",
                )
            else:
                logger.exception("FAIL | chat=%s url=%s", message.chat.id, url)
                await safe_edit_text(status, f"❌ Не удалось выполнить запрос:\n{e}")


async def main() -> None:
    logger.info(
        "BOT_START | download_dir=%s api_base=%s session_timeout=%ss quality_ttl=%ss delete_status=%s delay=%ss",
        str(DOWNLOAD_DIR),
        BOT_API_BASE_URL or "-",
        BOT_SESSION_TIMEOUT,
        QUALITY_MENU_TTL_SEC,
        DELETE_STATUS_ON_SUCCESS,
        DELETE_STATUS_DELAY_SEC,
    )

    # Local TDLib-based Bot API server support (telegram-bot-api container).
    # Поддержка локального Bot API сервера на TDLib (контейнер telegram-bot-api).
    #
    # RU: Если указан BOT_API_BASE_URL, бот отправляет запросы в локальный Bot API,
    #     что полезно для больших файлов и стабильности.
    # EN: If BOT_API_BASE_URL is set, bot uses local Bot API endpoint,
    #     which is useful for larger files and stability.
    if BOT_API_BASE_URL:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(BOT_API_BASE_URL, is_local=True),
            timeout=BOT_SESSION_TIMEOUT,
        )
        bot = Bot(token=BOT_TOKEN, session=session)
    else:
        bot = Bot(token=BOT_TOKEN)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
