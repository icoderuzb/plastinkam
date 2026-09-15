import asyncio
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from compose import build_disc
from database import (
    Channel,
    async_session,
    db_enqueue_job,
    db_recover_stale_jobs,
    db_update_job_status,
    get_analytics_stats,
    get_recent_job_count,
    get_user_preference,
    log_job_execution,
    set_user_watermark,
)
from processor import (
    concat_audio_files,
    extract_embedded_cover,
    get_duration,
    render_vinyl,
)
from states import AddChannelState, BatchAudioState, EditLabelTextState
from sub_service import check_subscriptions
from texts import (
    BTN_ADD_IMAGE,
    BTN_CANCEL,
    BTN_VINYL_BLUE,
    BTN_VINYL_DEFAULT,
    BTN_VINYL_PINK,
    BTN_VINYL_YELLOW,
    ERR_NO_THUMBNAIL_AVAILABLE,
    ERR_OUTPUT_NOT_CREATED,
    LOG_DELETE_FAILED_FMT,
    LOG_DOWNLOAD_RETRY_FAILED_FMT,
    LOG_FILE_TOO_LARGE,
    LOG_NO_DETAIL_MESSAGE,
    LOG_PROCESS_JOB_FAILED,
    LOG_PROGRESS_UPDATE_FAILED,
    LOG_QUEUE_PROCESS_FAILED,
    LOG_SEND_ERROR_FAILED,
    MSG_DEV_ONLY_OPTION,
    SPEED_LABEL_8RPM,
    SPEED_LABEL_33RPM,
    SPEED_LABEL_45RPM,
    SPEED_LABEL_FULL,
    STAGE_BUILDING_DISC,
    STAGE_DOWNLOADING_AUDIO,
    STAGE_DOWNLOADING_THUMBNAIL,
    STAGE_MERGING_AUDIO,
    STAGE_PREPARING,
    STAGE_RENDERING_VIDEO,
    STAGE_UPLOADING_VIDEO,
    fmt_emoji,
    get_btn_add_image,
    get_btn_batch_merge,
    get_btn_batch_separate,
    get_btn_cancel,
    get_btn_change_thumbnail_yes,
    get_btn_confirm_label_text,
    get_btn_continue_no_trim,
    get_btn_edit_label_text,
    get_btn_keep_thumbnail,
    get_btn_skip_label_text,
    get_btn_trim_preset_end,
    get_btn_trim_preset_middle,
    get_btn_trim_preset_start,
    get_btn_vinyl_blue,
    get_btn_vinyl_default,
    get_btn_vinyl_pink,
    get_btn_vinyl_yellow,
    get_btn_watermark_toggle,
    get_msg_audio_expired,
    get_msg_audio_received,
    get_msg_batch_detected,
    get_msg_batch_summary,
    get_msg_change_thumbnail_prompt,
    get_msg_dev_choose_template,
    get_msg_duration_too_long,
    get_msg_image_received,
    get_msg_job_queued,
    get_msg_label_text_input_request,
    get_msg_label_text_prompt,
    get_msg_label_text_updated,
    get_msg_no_pending_audio,
    get_msg_no_thumbnail_prompt,
    get_msg_processing_error,
    get_msg_queue_canceled_answer,
    get_msg_queue_canceled_edit,
    get_msg_rate_limited,
    get_msg_send_image_now,
    get_msg_speed_saved_answer,
    get_msg_start_help,
    get_msg_stats_report,
    get_msg_template_files_missing,
    get_msg_trim_accepted,
    get_msg_trim_invalid,
    get_msg_trim_prompt,
    get_msg_vinyl_choice_saved_answer,
    get_msg_vinyl_choice_saved_edit,
    get_msg_watermark_saved,
    get_msg_wrong_type,
    get_speed_label_8rpm,
    get_speed_label_33rpm,
    get_speed_label_45rpm,
    get_speed_label_full,
)

logger = logging.getLogger(__name__)
router = Router()

job_queue: asyncio.Queue[dict] = asyncio.Queue()
developer_job_queue: asyncio.Queue[dict] = asyncio.Queue()
worker_tasks: list[asyncio.Task] = []

pending_images: dict[int, dict] = {}
pending_audio: dict[int, dict] = {}
user_speed_choice: dict[int, str] = {}
user_vinyl_choice: dict[int, str] = {}
user_pending_jobs: dict[int, set[str]] = {}
tracked_jobs: dict[str, dict] = {}
canceled_job_ids: set[str] = set()
pending_trim: dict[int, dict] = {}

# Batch / Album mode collections
pending_batches: dict[int, dict] = {}
batch_debounce_tasks: dict[int, asyncio.Task] = {}

HOURGLASS_FRAMES = ["⏳", "⌛"]
PROGRESS_BAR_WIDTH = 12
STATUS_UPDATE_INTERVAL_SECONDS = 2.2


def render_progress_bar(percent: float, width: int = PROGRESS_BAR_WIDTH) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100))
    return "▓" * filled + "░" * (width - filled)


class StatusAnimator:
    """Telegramdagi holat xabarini davriy yangilaydi: qumsoat + progress bar."""

    def __init__(self, message: Message):
        self.message = message
        self.stage_text = STAGE_PREPARING
        self.percent: float | None = None
        self._frame = 0
        self._last_rendered: str | None = None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def set_stage(self, stage_text: str, percent: float | None = None) -> None:
        self.stage_text = stage_text
        self.percent = percent

    def _render(self) -> str:
        raw_frame = HOURGLASS_FRAMES[self._frame % len(HOURGLASS_FRAMES)]
        hourglass = fmt_emoji(raw_frame, config.EMOJI_HOURGLASS)
        if self.percent is not None:
            bar = render_progress_bar(self.percent)
            return f"{hourglass} {self.stage_text}\n{bar}  {int(self.percent)}%"
        dots = "." * ((self._frame % 3) + 1)
        return f"{hourglass} {self.stage_text}{dots}"

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            self._frame += 1
            text = self._render()
            if text != self._last_rendered:
                try:
                    await self.message.edit_text(text)
                    self._last_rendered = text
                except TelegramBadRequest:
                    pass
                except Exception:
                    logger.exception(LOG_PROGRESS_UPDATE_FAILED)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=STATUS_UPDATE_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass


def tmp(name: str) -> str:
    path = os.path.join(config.TEMP_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError as e:
            logger.warning(LOG_DELETE_FAILED_FMT.format(p=p, e=e))


def strip_unsupported_button_kwargs(keyboard: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    clean_rows = []
    for row in keyboard.inline_keyboard:
        clean_row = []
        for btn in row:
            kwargs = {}
            if btn.url:
                kwargs["url"] = btn.url
            if btn.callback_data:
                kwargs["callback_data"] = btn.callback_data
            clean_row.append(InlineKeyboardButton(text=btn.text, **kwargs))
        clean_rows.append(clean_row)
    return InlineKeyboardMarkup(inline_keyboard=clean_rows)


async def safe_reply_keyboard(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup
) -> Message:
    try:
        return await message.reply(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        logger.warning(
            "Reply with custom style keyboard failed (%s), retrying with clean keyboard...", exc
        )
        clean_markup = strip_unsupported_button_kwargs(reply_markup)
        return await message.reply(text, reply_markup=clean_markup)


def extract_rel_path(path_str: str) -> str:
    if not path_str:
        return ""
    p = path_str.replace("\\", "/")
    if "telegram-bot-api/" in p:
        p = p.split("telegram-bot-api/", 1)[1].lstrip("/")
        if "/" in p:
            p = p.split("/", 1)[1]
        return p
    if config.BOT_TOKEN and config.BOT_TOKEN in p:
        p = p.split(config.BOT_TOKEN, 1)[1].lstrip("/")
        return p
    p = re.sub(r"^.*?\d+:[^/]+/", "", p)
    return p.lstrip("/")


async def download_file_http(url: str, destination: str, timeout_seconds: int = 300) -> None:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(destination, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    f.write(chunk)


async def download_with_retries(
    bot: Bot, file_id: str, destination: str, timeout_seconds: int, retries: int = 3
) -> None:
    local_path = ""
    try:
        file_info = await bot.get_file(file_id)
        local_path = file_info.file_path or ""
    except Exception as e:
        logger.warning("bot.get_file(%s) failed: %s", file_id, e)

    if local_path and os.path.exists(local_path):
        try:
            import shutil as _shutil

            _shutil.copy2(local_path, destination)
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return
        except Exception as copy_err:
            logger.warning("Local file copy failed: %s", copy_err)

    rel_path = extract_rel_path(local_path)

    if rel_path and config.TELEGRAM_LOCAL_API_URL:
        local_http_url = (
            f"{config.TELEGRAM_LOCAL_API_URL.rstrip('/')}/file/bot{config.BOT_TOKEN}/{rel_path}"
        )
        try:
            await download_file_http(local_http_url, destination, timeout_seconds=timeout_seconds)
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return
        except Exception as http_err:
            logger.warning(
                "Local API HTTP download failed (%s), trying official API fallback...", http_err
            )

    if rel_path:
        official_url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{rel_path}"
        try:
            await download_file_http(official_url, destination, timeout_seconds=timeout_seconds)
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return
        except Exception as off_err:
            logger.warning("Official API fallback download failed: %s", off_err)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        if os.path.exists(destination):
            try:
                os.remove(destination)
            except OSError:
                pass
        try:
            await bot.download(
                file_id,
                destination=destination,
                timeout=timeout_seconds,
                chunk_size=64 * 1024,
            )
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                LOG_DOWNLOAD_RETRY_FAILED_FMT,
                attempt,
                retries,
                type(exc).__name__,
                exc or LOG_NO_DETAIL_MESSAGE,
            )
            if attempt < retries:
                await asyncio.sleep(2)
            else:
                raise
    if last_error is not None:
        raise last_error


# ============================================================
# Persistent Queue & Worker Architecture (Features 5, 6, 7)
# ============================================================

async def _worker(bot: Bot) -> None:
    while True:
        queue = None
        try:
            job = developer_job_queue.get_nowait()
            queue = developer_job_queue
        except asyncio.QueueEmpty:
            try:
                job = job_queue.get_nowait()
                queue = job_queue
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
                continue

        job_id = job.get("job_id")
        uid = job.get("uid", 0)
        try:
            if job_id in canceled_job_ids:
                canceled_job_ids.discard(job_id)
                tracked_jobs.pop(job_id, None)
                user_pending_jobs.get(uid, set()).discard(job_id)
                async with async_session() as db_sess:
                    await db_update_job_status(db_sess, job_id, "canceled")
                continue

            tracked_jobs[job_id] = job
            async with async_session() as db_sess:
                await db_update_job_status(db_sess, job_id, "processing")

            await process_job(bot, job)

            async with async_session() as db_sess:
                await db_update_job_status(db_sess, job_id, "done")
        except Exception as exc:
            logger.exception(LOG_QUEUE_PROCESS_FAILED)
            async with async_session() as db_sess:
                await db_update_job_status(db_sess, job_id, "failed")
        finally:
            tracked_jobs.pop(job_id, None)
            user_pending_jobs.get(uid, set()).discard(job_id)
            if queue is not None:
                queue.task_done()


async def start_job_worker(bot: Bot) -> None:
    """Ishchi workerlarni va crash recovery tizimini ishga tushiradi."""
    worker_tasks[:] = [t for t in worker_tasks if not t.done()]
    target = max(1, config.MAX_CONCURRENT_JOBS)

    # Crash recovery: oldingi qulashdan qolgan 'processing' vazifalarni tozalab 'queued' ga qaytarish
    try:
        async with async_session() as db_sess:
            stale_jobs = await db_recover_stale_jobs(db_sess)
            if stale_jobs:
                logger.info("%s ta qolib ketgan vazifa persistent navbatda tiklandi.", len(stale_jobs))
    except Exception as e:
        logger.warning("Crash recovery xatoligi: %s", e)

    while len(worker_tasks) < target:
        worker_tasks.append(asyncio.create_task(_worker(bot)))


def get_user_speed_key(user_id: int) -> str:
    return user_speed_choice.get(user_id, "33")


def get_user_rotation_seconds(user_id: int) -> float | None:
    key = get_user_speed_key(user_id)
    if key == "full":
        return 0.0
    elif key == "8":
        return 60 / 8.0
    elif key == "33":
        return 60 / 33.333333333333336
    elif key == "45":
        return 60 / 45.0
    return config.ROTATION_SECONDS


def get_user_vinyl_path(user_id: int) -> str:
    choice = user_vinyl_choice.get(user_id, "default")
    if choice == "pink":
        return config.VINYL_PINK_PATH
    if choice == "yellow":
        return config.VINYL_YELLOW_PATH
    if choice == "blue":
        return config.VINYL_BLUE_PATH
    return config.VINYL_PATH


def get_user_shadow_path(user_id: int) -> str:
    choice = user_vinyl_choice.get(user_id, "default")
    if choice == "pink":
        return config.SHADOW_PINK_PATH
    if choice == "yellow":
        return config.SHADOW_YELLOW_PATH
    if choice == "blue":
        return config.SHADOW_BLUE_PATH
    return config.SHADOW_PATH


def get_job_priority(user_id: int) -> int:
    return 0 if user_id and user_id == config.DEVELOPER_ID else 1


async def enqueue_job(job: dict) -> None:
    job_id = job.get("job_id")
    uid = job.get("uid", 0)
    priority = get_job_priority(uid)

    # Persistent SQLite DB ga saqlash
    try:
        async with async_session() as db_sess:
            await db_enqueue_job(
                db_sess,
                job_id=job_id,
                user_id=uid,
                priority=priority,
                payload_data={
                    "job_type": job.get("job_type", "single"),
                    "start_offset": job.get("start_offset", 0.0),
                    "artist": job.get("artist"),
                    "title": job.get("title"),
                    "include_watermark": job.get("include_watermark", False),
                },
            )
    except Exception as e:
        logger.warning("DB enqueue job failed: %s", e)

    if priority == 0:
        developer_job_queue.put_nowait(job)
    else:
        job_queue.put_nowait(job)


def cancel_user_jobs(user_id: int) -> None:
    pending_ids = user_pending_jobs.pop(user_id, set())
    for job_id in list(pending_ids):
        canceled_job_ids.add(job_id)
        job = tracked_jobs.pop(job_id, None)
        if job:
            cleanup(*job.get("temp_paths", []))
    _MAX_CANCELED_IDS = 500
    if len(canceled_job_ids) > _MAX_CANCELED_IDS:
        overflow = len(canceled_job_ids) - _MAX_CANCELED_IDS
        for old_id in list(canceled_job_ids)[:overflow]:
            canceled_job_ids.discard(old_id)


# ============================================================
# Main Processing Pipeline (Single & Batch Jobs)
# ============================================================

async def process_job(bot: Bot, job: dict) -> None:
    start_time = time.time()
    message: Message = job["message"]
    uid = job["uid"]
    job_id = job["job_id"]
    job_type = job.get("job_type", "single")

    status = await message.reply(get_msg_audio_received(config.EMOJI_HOURGLASS))
    animator = StatusAnimator(status)
    animator.start()

    is_success = False
    error_message: Optional[str] = None
    processed_duration = 0.0

    audio_path = tmp(f"{uid}_{job_id}_audio.mp3")
    thumb_path = tmp(f"{uid}_{job_id}_thumb.jpg")
    disc_path = tmp(f"{uid}_{job_id}_disc.png")
    out_path = tmp(f"{uid}_{job_id}_out.mp4")
    job["temp_paths"] = [audio_path, thumb_path, disc_path, out_path]

    try:
        await bot.send_chat_action(message.chat.id, action=ChatAction.RECORD_VIDEO_NOTE)

        # 1. Audio fayl(lar)ni yuklab olish
        animator.set_stage(STAGE_DOWNLOADING_AUDIO)
        if job_type == "batch_merge":
            # Bir nechta audiolarni yuklab olib, birlashtirish
            animator.set_stage(STAGE_MERGING_AUDIO)
            audio_items = job.get("audio_items", [])
            downloaded_parts: List[str] = []
            for idx, item in enumerate(audio_items):
                part_path = tmp(f"{uid}_{job_id}_part_{idx}.mp3")
                job["temp_paths"].append(part_path)
                await download_with_retries(bot, item.file_id, part_path, timeout_seconds=300, retries=2)
                downloaded_parts.append(part_path)

            await concat_audio_files(downloaded_parts, audio_path, max_duration=config.MAX_DURATION_SECONDS)
            audio = audio_items[0] if audio_items else None
        else:
            audio = job["audio"]
            await download_with_retries(bot, audio.file_id, audio_path, timeout_seconds=300, retries=3)

        # 2. Muqova rasmini olish (User photo -> Telegram thumb -> Embedded ID3)
        thumbnail_file_id = job.get("thumbnail_file_id")
        if not thumbnail_file_id and audio:
            thumb_obj = getattr(audio, "thumbnail", None) or getattr(audio, "thumb", None)
            if thumb_obj is not None:
                thumbnail_file_id = getattr(thumb_obj, "file_id", None)

        thumb_obtained = False
        if thumbnail_file_id:
            animator.set_stage(STAGE_DOWNLOADING_THUMBNAIL)
            try:
                await download_with_retries(bot, thumbnail_file_id, thumb_path, timeout_seconds=60, retries=2)
                if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                    thumb_obtained = True
            except Exception as e:
                logger.warning("Thumbnail download failed: %s", e)

        if not thumb_obtained:
            animator.set_stage(STAGE_DOWNLOADING_THUMBNAIL)
            extracted = await extract_embedded_cover(audio_path, thumb_path)
            if extracted:
                thumb_obtained = True

        if not thumb_obtained:
            # Thumbnail mutlaqo topilmadi — foydalanuvchidan so'rash
            await animator.stop()
            try:
                await status.delete()
            except Exception:
                pass

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=get_btn_add_image(config.BTN_EMOJI_ADD_IMAGE),
                    callback_data="add_image",
                    style="primary",
                    icon_custom_emoji_id=config.BTN_EMOJI_ADD_IMAGE or None,
                )],
                [InlineKeyboardButton(
                    text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                    callback_data="cancel_queue",
                    style="danger",
                    icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
                )],
            ])

            await safe_reply_keyboard(
                message,
                get_msg_no_thumbnail_prompt(config.EMOJI_WARNING),
                reply_markup=keyboard,
            )

            pending_audio[uid] = {
                "audio": audio,
                "message": message,
                "expires_at": time.time() + 300,
                "job_id": job_id,
                "uid": uid,
                "has_thumbnail": False,
                "artist": job.get("artist"),
                "title": job.get("title"),
                "include_watermark": job.get("include_watermark", False),
            }
            pending_images[uid] = {"waiting_for_image": True, "audio_message_id": message.message_id}
            cleanup(audio_path, thumb_path, disc_path, out_path)
            return

        duration = await get_duration(audio_path)
        processed_duration = min(duration, config.MAX_DURATION_SECONDS)
        start_offset = job.get("start_offset", 0.0)

        # 3. Disk kompozitsiyasi (Muqova + Label Text + Watermark)
        await bot.send_chat_action(message.chat.id, action=ChatAction.UPLOAD_VIDEO_NOTE)
        animator.set_stage(STAGE_BUILDING_DISC)

        artist = job.get("artist")
        title = job.get("title")
        include_watermark = job.get("include_watermark", False)
        vinyl_color = user_vinyl_choice.get(uid, "default")

        await asyncio.to_thread(
            build_disc,
            thumb_path=thumb_path,
            vinyl_path=get_user_vinyl_path(uid),
            out_path=disc_path,
            hole_ratio=config.HOLE_RATIO,
            size=config.DISC_SIZE,
            artist=artist,
            title=title,
            vinyl_color=vinyl_color,
            include_watermark=include_watermark,
        )

        # 4. FFmpeg Video Render
        animator.set_stage(STAGE_RENDERING_VIDEO, percent=0)

        async def on_render_progress(percent: float) -> None:
            animator.set_stage(STAGE_RENDERING_VIDEO, percent=percent)

        await render_vinyl(
            disc_path=disc_path,
            shadow_path=get_user_shadow_path(uid),
            audio_path=audio_path,
            out_path=out_path,
            rotation_seconds=get_user_rotation_seconds(uid),
            size=config.DISC_SIZE,
            fps=config.OUTPUT_FPS,
            max_duration=config.MAX_DURATION_SECONDS,
            start_offset=start_offset,
            on_progress=on_render_progress,
        )

        if not os.path.exists(out_path):
            raise FileNotFoundError(ERR_OUTPUT_NOT_CREATED)

        # 5. Telegramga yuborish
        animator.set_stage(STAGE_UPLOADING_VIDEO, percent=100)
        await bot.send_chat_action(message.chat.id, action=ChatAction.UPLOAD_VIDEO_NOTE)
        await message.reply_video_note(FSInputFile(out_path), length=config.DISC_SIZE)
        is_success = True

    except Exception as e:
        logger.exception(LOG_PROCESS_JOB_FAILED)
        error_message = str(e) or repr(e)
        try:
            await message.reply(get_msg_processing_error(error_message, config.EMOJI_ERROR))
        except Exception:
            logger.exception(LOG_SEND_ERROR_FAILED)
    finally:
        await animator.stop()
        cleanup(*job.get("temp_paths", []))
        try:
            await status.delete()
        except Exception:
            pass

        # 6. DB ga jurnal (Log) yozish
        processing_time = round(time.time() - start_time, 2)
        try:
            async with async_session() as db_sess:
                await log_job_execution(
                    db_sess,
                    user_id=uid,
                    job_type=job_type,
                    vinyl_color=user_vinyl_choice.get(uid, "default"),
                    rotation_speed=get_user_speed_key(uid),
                    duration_seconds=processed_duration,
                    is_success=is_success,
                    processing_time_seconds=processing_time,
                    error_message=error_message,
                )
        except Exception as log_err:
            logger.warning("Job logging failed: %s", log_err)


# ============================================================
# Keyboards & Helpers
# ============================================================

def build_speed_keyboard(user_id: int) -> InlineKeyboardMarkup:
    current_key = get_user_speed_key(user_id)
    has_speed_emoji = bool(
        config.BTN_EMOJI_SPEED_ACTIVE or config.BTN_EMOJI_SPEED_INACTIVE or config.BTN_EMOJI_SPEED
    )
    labels = [
        (get_speed_label_full("yes" if has_speed_emoji else None), "full"),
        (get_speed_label_8rpm("yes" if has_speed_emoji else None), "8"),
        (get_speed_label_33rpm("yes" if has_speed_emoji else None), "33"),
        (get_speed_label_45rpm("yes" if has_speed_emoji else None), "45"),
    ]
    buttons = []
    for label, value in labels:
        selected = current_key == value
        btn_style = "success" if selected else "primary"
        btn_emoji = config.BTN_EMOJI_SPEED_ACTIVE if selected else config.BTN_EMOJI_SPEED_INACTIVE
        if not btn_emoji:
            btn_emoji = config.BTN_EMOJI_SPEED

        check_mark = " ✅" if (selected and not btn_emoji) else ""
        buttons.append(
            InlineKeyboardButton(
                text=f"{label}{check_mark}",
                callback_data=f"speed:{value}",
                style=btn_style,
                icon_custom_emoji_id=btn_emoji or None,
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


def build_vinyl_keyboard(selected_choice: str | None = None) -> InlineKeyboardMarkup:
    options = [
        ("pink", get_btn_vinyl_pink(config.BTN_EMOJI_VINYL_PINK), config.BTN_EMOJI_VINYL_PINK, "primary"),
        ("default", get_btn_vinyl_default(config.BTN_EMOJI_VINYL_DEFAULT), config.BTN_EMOJI_VINYL_DEFAULT, "danger"),
        ("yellow", get_btn_vinyl_yellow(config.BTN_EMOJI_VINYL_YELLOW), config.BTN_EMOJI_VINYL_YELLOW, "primary"),
        ("blue", get_btn_vinyl_blue(config.BTN_EMOJI_VINYL_BLUE), config.BTN_EMOJI_VINYL_BLUE, "primary"),
    ]
    rows = []
    for choice_key, text, emoji_id, default_style in options:
        selected = (selected_choice == choice_key)
        style = "success" if selected else default_style
        clean_text = text.replace("✅ ", "").strip()
        btn_text = f"✅ {clean_text}" if selected else clean_text
        rows.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"vinyl:{choice_key}",
                style=style,
                icon_custom_emoji_id=emoji_id or None,
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_trim_keyboard(duration: float) -> InlineKeyboardMarkup:
    """Audio kesish uchun qulay preset tugmalar klaviaturasi (Feature 4)."""
    dur_int = int(duration)
    mid_start = max(0, (dur_int // 2) - 30)
    mid_end = min(dur_int, mid_start + 60)
    end_start = max(0, dur_int - 60)

    buttons = [
        [
            InlineKeyboardButton(
                text=get_btn_trim_preset_start(),
                callback_data="trim_preset:0:60",
                style="success",
            ),
            InlineKeyboardButton(
                text=get_btn_trim_preset_middle(),
                callback_data=f"trim_preset:{mid_start}:{mid_end}",
                style="primary",
            ),
        ],
        [
            InlineKeyboardButton(
                text=get_btn_trim_preset_end(),
                callback_data=f"trim_preset:{end_start}:{dur_int}",
                style="primary",
            ),
            InlineKeyboardButton(
                text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                callback_data="cancel_queue",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_confirmation_keyboard(include_watermark: bool, has_metadata: bool = True) -> InlineKeyboardMarkup:
    """Thumbnail, Label Text va Watermark tasdiqlash klaviaturasi (Features 1 & 3)."""
    rows = [
        [
            InlineKeyboardButton(
                text=get_btn_change_thumbnail_yes(config.BTN_EMOJI_ADD_IMAGE),
                callback_data="change_thumb",
                style="primary",
                icon_custom_emoji_id=config.BTN_EMOJI_ADD_IMAGE or None,
            ),
            InlineKeyboardButton(
                text=get_btn_keep_thumbnail(config.BTN_EMOJI_CONTINUE),
                callback_data="keep_thumb",
                style="success",
                icon_custom_emoji_id=config.BTN_EMOJI_CONTINUE or None,
            ),
        ],
    ]

    if config.ENABLE_LABEL_TEXT:
        edit_btn_text = get_btn_edit_label_text() if has_metadata else "✍️ Matn kiritish"
        label_row = [
            InlineKeyboardButton(
                text=edit_btn_text,
                callback_data="label_edit",
                style="primary",
            )
        ]
        if has_metadata:
            label_row.append(
                InlineKeyboardButton(
                    text=get_btn_skip_label_text(),
                    callback_data="label_skip",
                    style="primary",
                )
            )
        rows.append(label_row)

    rows.append([
        InlineKeyboardButton(
            text=get_btn_watermark_toggle(include_watermark),
            callback_data="toggle_watermark",
            style="primary",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
            callback_data="cancel_queue",
            style="danger",
            icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# Basic Commands (/start, /help, /rang, /dev, /stats, /channels)
# ============================================================

@router.message(F.text.in_({"/start", "/help"}))
async def on_start(message: Message):
    await message.reply(
        get_msg_start_help(config.EMOJI_SPEED),
        reply_markup=build_speed_keyboard(message.from_user.id if message.from_user else 0),
    )


@router.message(F.text == "/rang")
async def on_rang(message: Message, session: AsyncSession):
    if not message.from_user:
        return
    uid = message.from_user.id
    pref = await get_user_preference(session, uid)
    current = user_vinyl_choice.get(uid, pref.preferred_vinyl or "default")
    user_vinyl_choice[uid] = current
    await message.reply(
        get_msg_dev_choose_template(config.EMOJI_PALETTE),
        reply_markup=build_vinyl_keyboard(current),
    )


@router.message(F.text == "/dev")
async def on_dev(message: Message, session: AsyncSession):
    if not message.from_user or message.from_user.id != config.DEVELOPER_ID:
        return
    uid = message.from_user.id
    pref = await get_user_preference(session, uid)
    current = user_vinyl_choice.get(uid, pref.preferred_vinyl or "default")
    user_vinyl_choice[uid] = current
    await message.reply(
        get_msg_dev_choose_template(config.EMOJI_PALETTE),
        reply_markup=build_vinyl_keyboard(current),
    )



@router.message(F.text == "/stats")
async def on_stats(message: Message, session: AsyncSession):
    """Admin uchun batafsil statistika hisoboti (Feature 5)."""
    if not message.from_user or message.from_user.id != config.DEVELOPER_ID:
        return

    stats = await get_analytics_stats(session)
    await message.reply(get_msg_stats_report(stats, config.EMOJI_STATS))


# ============================================================
# Channel Administration & Force-Sub (/channels, check_sub)
# ============================================================

@router.callback_query(F.data == "check_sub")
async def on_check_sub(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return

    user_id = callback.from_user.id
    unsubscribed = await check_subscriptions(bot=bot, user_id=user_id, session=session)

    if unsubscribed:
        await callback.answer("❌ Hali barcha kanallarga a'zo bo'lmadingiz!", show_alert=True)
    else:
        await callback.answer("🎉 Rahmat! A'zolik tasdiqlandi.")
        if callback.message:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(
                get_msg_start_help(config.EMOJI_SPEED),
                reply_markup=build_speed_keyboard(user_id),
            )


@router.message(F.text == "/channels")
async def show_channels_admin(message: Message, session: AsyncSession):
    if not message.from_user or message.from_user.id != config.DEVELOPER_ID:
        return

    stmt = select(Channel)
    result = await session.execute(stmt)
    channels = list(result.scalars().all())

    buttons = []
    for ch in channels:
        buttons.append([
            InlineKeyboardButton(text=f"📢 {ch.name}", url=ch.url),
            InlineKeyboardButton(
                text="🗑 O'chirish",
                callback_data=f"del_ch_{ch.id}",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="➕ Yangi kanal qo'shish",
            callback_data="add_channel",
            style="primary",
            icon_custom_emoji_id=config.BTN_EMOJI_ADD_IMAGE or None,
        )
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.reply("⚙️ <b>Majburiy kanallarni boshqarish paneli:</b>", reply_markup=kb)


@router.callback_query(F.data == "add_channel")
async def start_add_channel(callback: CallbackQuery, state: FSMContext):
    if not callback.from_user or callback.from_user.id != config.DEVELOPER_ID:
        await callback.answer(MSG_DEV_ONLY_OPTION)
        return

    await state.set_state(AddChannelState.waiting_for_channel_info)
    await callback.message.reply(
        "➕ <b>Yangi kanal qo'shish uchun ma'lumotni kiriting:</b>\n\n"
        "Format: <code>Kanal Nomi - https://t.me/kanal_link</code>\n\n"
        "Misol: <code>Mening Kanalim - https://t.me/my_channel</code>"
    )
    await callback.answer()


@router.message(AddChannelState.waiting_for_channel_info)
async def process_add_channel(message: Message, state: FSMContext, session: AsyncSession):
    if not message.from_user or message.from_user.id != config.DEVELOPER_ID:
        return

    text = (message.text or "").strip()
    if " - " not in text:
        await message.reply(
            "❌ Noto'g'ri format! Iltimos, <code>Kanal Nomi - https://t.me/link</code> shaklida yuboring."
        )
        return

    name, url = text.split(" - ", 1)
    new_channel = Channel(name=name.strip(), url=url.strip())
    session.add(new_channel)
    await session.commit()

    await state.clear()
    await message.reply(f"✅ <b>'{name.strip()}'</b> kanali muvaffaqiyatli bazaga qo'shildi!")


@router.callback_query(F.data.startswith("del_ch_"))
async def delete_channel_callback(callback: CallbackQuery, session: AsyncSession):
    if not callback.from_user or callback.from_user.id != config.DEVELOPER_ID:
        await callback.answer(MSG_DEV_ONLY_OPTION)
        return

    channel_id = int(callback.data.split("del_ch_")[1])
    stmt = delete(Channel).where(Channel.id == channel_id)
    await session.execute(stmt)
    await session.commit()

    await callback.answer("🗑 Kanal o'chirildi!", show_alert=True)
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass


# ============================================================
# Audio Message Entry Point & Batch Dispatcher (Features 2, 8)
# ============================================================

async def _trigger_batch_decision(user_id: int, bot: Bot):
    """Debounce taymeri tugagach, foydalanuvchiga albom rejimi tanlovini ko'rsatish."""
    await asyncio.sleep(config.BATCH_DEBOUNCE_SECONDS)
    batch = pending_batches.get(user_id)
    if not batch:
        return

    items = batch["items"]
    initial_msg: Message = batch["message"]
    count = len(items)

    if count == 1:
        # Faqat bitta audio kelgan — standart yakka audio oqimiga yo'naltirish
        pending_batches.pop(user_id, None)
        await _handle_single_audio(initial_msg, items[0], bot)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=get_btn_batch_separate(),
                callback_data="batch_sep",
                style="primary",
            ),
            InlineKeyboardButton(
                text=get_btn_batch_merge(),
                callback_data="batch_merge",
                style="success",
            ),
        ],
        [
            InlineKeyboardButton(
                text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                callback_data="cancel_queue",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            )
        ],
    ])

    await safe_reply_keyboard(
        initial_msg,
        get_msg_batch_detected(count, config.EMOJI_BATCH),
        reply_markup=keyboard,
    )


@router.message(F.audio | F.document)
async def on_audio(message: Message, bot: Bot, session: AsyncSession):
    if not message.from_user:
        return

    uid = message.from_user.id

    # 1. Rate Limiting Check (Feature 8)
    if uid != config.DEVELOPER_ID:
        recent_count = await get_recent_job_count(session, uid, config.RATE_LIMIT_WINDOW_SECONDS)
        if recent_count >= config.RATE_LIMIT_JOBS:
            await message.reply(get_msg_rate_limited(config.RATE_LIMIT_WINDOW_SECONDS, config.EMOJI_HOURGLASS))
            return

    # 2. Audio type filtering
    if message.document:
        doc = message.document
        mime = doc.mime_type or ""
        fname = doc.file_name or ""
        is_audio = mime.startswith("audio/") or fname.lower().endswith(
            (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus", ".wma")
        )
        if not is_audio:
            return
        audio = doc
    else:
        audio = message.audio

    if not audio:
        return

    if not os.path.exists(config.VINYL_PATH) or not os.path.exists(config.SHADOW_PATH):
        await message.reply(get_msg_template_files_missing(config.EMOJI_WARNING))
        return

    file_size = getattr(audio, "file_size", None)
    if file_size and file_size > config.MAX_TELEGRAM_AUDIO_SIZE_BYTES:
        logger.info(LOG_FILE_TOO_LARGE)
        await message.reply(get_msg_processing_error(LOG_FILE_TOO_LARGE, config.EMOJI_WARNING))
        return

    # 3. Batch / Media Group Collector (Feature 2)
    if uid in pending_batches:
        pending_batches[uid]["items"].append(audio)
        if uid in batch_debounce_tasks:
            batch_debounce_tasks[uid].cancel()
        batch_debounce_tasks[uid] = asyncio.create_task(_trigger_batch_decision(uid, bot))
        return
    elif message.media_group_id:
        pending_batches[uid] = {
            "message": message,
            "items": [audio],
            "media_group_id": message.media_group_id,
        }
        batch_debounce_tasks[uid] = asyncio.create_task(_trigger_batch_decision(uid, bot))
        return

    # Yakka audio
    await _handle_single_audio(message, audio, bot)


async def _handle_single_audio(message: Message, audio: Any, bot: Bot):
    """Yakka audio faylni tasdiqlash va ishlov berish bosqichi."""
    uid = message.from_user.id
    job_id = uuid.uuid4().hex

    duration = getattr(audio, "duration", 0) or 0
    performer = getattr(audio, "performer", None) or ""
    title = getattr(audio, "title", None) or ""

    # User watermark preference
    async with async_session() as db_sess:
        pref = await get_user_preference(db_sess, uid)
        include_watermark = pref.include_watermark

    # 1. Trim Check (Feature 4)
    if duration > config.MAX_DURATION_SECONDS:
        pending_trim[uid] = {
            "audio": audio,
            "message": message,
            "duration": duration,
            "job_id": job_id,
            "uid": uid,
            "artist": performer,
            "title": title,
            "include_watermark": include_watermark,
            "expires_at": time.time() + 300,
        }
        await safe_reply_keyboard(
            message,
            get_msg_trim_prompt(duration, config.EMOJI_TRIM),
            reply_markup=build_trim_keyboard(duration),
        )
        return

    # 2. Cover / Label Text / Watermark Confirmation
    has_thumb = bool(getattr(audio, "thumbnail", None) or getattr(audio, "thumb", None))
    prompt_msg = get_msg_change_thumbnail_prompt(
        emoji_id_camera=config.EMOJI_CAMERA,
        emoji_id_music=config.EMOJI_MUSIC,
    )
    if performer or title:
        prompt_msg += f"\n\n✍️ <i>Aniqlangan matn:</i> <b>{performer}</b> — <i>{title}</i>"

    keyboard = build_confirmation_keyboard(include_watermark=include_watermark, has_metadata=bool(performer or title))
    await safe_reply_keyboard(message, prompt_msg, reply_markup=keyboard)

    pending_audio[uid] = {
        "audio": audio,
        "message": message,
        "expires_at": time.time() + 300,
        "job_id": job_id,
        "uid": uid,
        "has_thumbnail": has_thumb,
        "artist": performer,
        "title": title,
        "include_watermark": include_watermark,
    }
    pending_images[uid] = {"audio_message_id": message.message_id}


# ============================================================
# Batch Callback Handlers (Feature 2: Album Mode)
# ============================================================

@router.callback_query(F.data == "batch_sep")
async def on_batch_separate(callback: CallbackQuery, bot: Bot):
    """Albomdagi har bir audioni alohida-alohida qayta ishlash."""
    if not callback.from_user:
        await callback.answer()
        return

    uid = callback.from_user.id
    batch = pending_batches.pop(uid, None)
    if not batch:
        await callback.answer("To'plam topilmadi", show_alert=True)
        return

    items = batch["items"]
    msg = batch["message"]
    try:
        await callback.message.edit_text(
            f"🧵 <b>{len(items)} ta audio navbatga qo'shildi</b>, har biri alohida tayyorlanadi."
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

    await start_job_worker(bot)
    for item in items:
        j_id = uuid.uuid4().hex
        perf = getattr(item, "performer", "") or ""
        title = getattr(item, "title", "") or ""
        job = {
            "message": msg,
            "audio": item,
            "uid": uid,
            "job_id": j_id,
            "artist": perf,
            "title": title,
            "job_type": "single",
        }
        tracked_jobs[j_id] = job
        user_pending_jobs.setdefault(uid, set()).add(j_id)
        await enqueue_job(job)


@router.callback_query(F.data == "batch_merge")
async def on_batch_merge(callback: CallbackQuery, bot: Bot):
    """Albomdagi barcha audiolarni bitta 60s video-xabarga birlashtirish."""
    if not callback.from_user:
        await callback.answer()
        return

    uid = callback.from_user.id
    batch = pending_batches.pop(uid, None)
    if not batch:
        await callback.answer("To'plam topilmadi", show_alert=True)
        return

    items = batch["items"]
    msg = batch["message"]
    j_id = uuid.uuid4().hex

    try:
        await callback.message.edit_text(
            f"🔗 <b>{len(items)} ta audio bitta miks videoga birlashtirilmoqda...</b>"
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

    await start_job_worker(bot)
    job = {
        "message": msg,
        "audio_items": items,
        "uid": uid,
        "job_id": j_id,
        "job_type": "batch_merge",
        "artist": "MIX",
        "title": f"{len(items)} Tracks",
    }
    tracked_jobs[j_id] = job
    user_pending_jobs.setdefault(uid, set()).add(j_id)
    await enqueue_job(job)


# ============================================================
# Trim & Presets Handlers (Feature 4)
# ============================================================

@router.callback_query(F.data.startswith("trim_preset:"))
async def on_trim_preset(callback: CallbackQuery, bot: Bot):
    """Tugmalar orqali belgilangan trim oralig'ini tanlash."""
    if not callback.from_user:
        await callback.answer()
        return

    uid = callback.from_user.id
    trim_data = pending_trim.pop(uid, None)
    if not trim_data:
        await callback.answer("Kutilayotgan audio topilmadi", show_alert=True)
        return

    parts = callback.data.split(":")
    start = int(parts[1])
    end = int(parts[2])

    try:
        await callback.message.edit_text(get_msg_trim_accepted(start, end, config.EMOJI_SUCCESS))
    except TelegramBadRequest:
        pass
    await callback.answer()


    await start_job_worker(bot)
    job = {
        "message": trim_data["message"],
        "audio": trim_data["audio"],
        "uid": uid,
        "job_id": trim_data["job_id"],
        "trim_handled": True,
        "start_offset": float(start),
        "artist": trim_data.get("artist"),
        "title": trim_data.get("title"),
        "include_watermark": trim_data.get("include_watermark", False),
    }
    if trim_data.get("thumbnail_file_id"):
        job["thumbnail_file_id"] = trim_data["thumbnail_file_id"]

    tracked_jobs[job["job_id"]] = job
    user_pending_jobs.setdefault(uid, set()).add(job["job_id"])
    await enqueue_job(job)


@router.message(F.text)
async def on_text_message(message: Message, bot: Bot, state: FSMContext):
    """Qo'lda kiritilgan 'start:end' trim formati yoki FSM matnlarini qabul qilish."""
    if not message.from_user:
        return
    uid = message.from_user.id

    raw_text = (message.text or "").strip()

    # 1. EditLabelTextState FSM tekshiruvi (Feature 1) yoki pending_audio kutilayotganda matn kiritish
    current_state = await state.get_state()
    is_label_state = (current_state == EditLabelTextState.waiting_for_text)
    pending_entry = pending_audio.get(uid)
    trim_data = pending_trim.get(uid)
    is_trim_format = bool(re.match(r"^(\d+)\s*:\s*(\d+)$", raw_text))

    if is_label_state or (pending_entry and not trim_data and not is_trim_format):
        artist = ""
        title = ""

        # 2 qatorli format: 1-qator: Ijrochi, 2-qator: Qo'shiq nomi
        if "\n" in raw_text:
            lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
            if len(lines) >= 2:
                artist = lines[0]
                title = lines[1]
            elif len(lines) == 1:
                title = lines[0]
        # " - ", " — ", " / " kabi ajratuvchilar
        elif " - " in raw_text or " — " in raw_text or " / " in raw_text:
            delim = " - " if " - " in raw_text else (" — " if " — " in raw_text else " / ")
            parts = raw_text.split(delim, 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        else:
            # Bitta satr bo'lsa — qo'shiq nomi
            title = raw_text

        include_wm = False
        if pending_entry:
            pending_entry["artist"] = artist
            pending_entry["title"] = title
            include_wm = pending_entry.get("include_watermark", False)

        await state.clear()
        kb = build_confirmation_keyboard(include_watermark=include_wm, has_metadata=bool(artist or title))
        await message.reply(get_msg_label_text_updated(artist, title, config.EMOJI_SUCCESS), reply_markup=kb)
        return

    # 2. Qo'lda Trim oraliq kiritish (masalan 15:75)
    trim_data = pending_trim.get(uid)
    if not trim_data:
        return

    text = (message.text or "").strip()
    match = re.match(r"^(\d+)\s*:\s*(\d+)$", text)
    if not match:
        await message.reply(get_msg_trim_invalid(config.EMOJI_WARNING))
        return

    start = int(match.group(1))
    end = int(match.group(2))
    audio_dur = trim_data["duration"]

    if start < 0 or end <= start or start >= audio_dur:
        await message.reply(get_msg_trim_invalid(config.EMOJI_WARNING))
        return

    actual_end = min(end, start + int(config.MAX_DURATION_SECONDS))
    actual_end = min(actual_end, audio_dur)

    pending_trim.pop(uid, None)
    await message.reply(get_msg_trim_accepted(start, actual_end, config.EMOJI_SUCCESS))

    await start_job_worker(bot)
    job = {
        "message": trim_data["message"],
        "audio": trim_data["audio"],
        "uid": uid,
        "job_id": trim_data["job_id"],
        "trim_handled": True,
        "start_offset": float(start),
        "artist": trim_data.get("artist"),
        "title": trim_data.get("title"),
        "include_watermark": trim_data.get("include_watermark", False),
    }
    if trim_data.get("thumbnail_file_id"):
        job["thumbnail_file_id"] = trim_data["thumbnail_file_id"]

    tracked_jobs[job["job_id"]] = job
    user_pending_jobs.setdefault(uid, set()).add(job["job_id"])
    await enqueue_job(job)


# ============================================================
# Watermark & Label Text Callback Handlers (Features 1 & 3)
# ============================================================

@router.callback_query(F.data == "toggle_watermark")
async def on_toggle_watermark(callback: CallbackQuery, session: AsyncSession):
    """Watermark tanlovini yoqish/o'chirish va DB ga saqlash."""
    if not callback.from_user:
        await callback.answer()
        return

    uid = callback.from_user.id
    pref = await get_user_preference(session, uid)
    new_state = not pref.include_watermark
    await set_user_watermark(session, uid, new_state)

    pending_entry = pending_audio.get(uid)
    if pending_entry:
        pending_entry["include_watermark"] = new_state

    # Klaviaturani yangilash
    try:
        kb = build_confirmation_keyboard(
            include_watermark=new_state,
            has_metadata=bool(pending_entry.get("artist") or pending_entry.get("title")) if pending_entry else True,
        )
        await callback.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass

    await callback.answer(get_msg_watermark_saved(new_state, config.EMOJI_SUCCESS))


@router.callback_query(F.data == "label_edit")
async def on_label_edit(callback: CallbackQuery, state: FSMContext):
    """Plastinka matnini tahrirlash rejimiga o'tish."""
    if not callback.from_user:
        await callback.answer()
        return

    await state.set_state(EditLabelTextState.waiting_for_text)
    await callback.message.reply(get_msg_label_text_input_request(config.EMOJI_LABEL))
    await callback.answer()


@router.callback_query(F.data == "label_skip")
async def on_label_skip(callback: CallbackQuery):
    """Plastinka matnini o'chirib, toza disk chiqarish."""
    if not callback.from_user:
        await callback.answer()
        return

    uid = callback.from_user.id
    pending_entry = pending_audio.get(uid)
    if pending_entry:
        pending_entry["artist"] = ""
        pending_entry["title"] = ""

    await callback.answer("⏩ Matnsiz chiqarish tanlandi")
    if callback.message:
        try:
            kb = build_confirmation_keyboard(
                include_watermark=pending_entry.get("include_watermark", False) if pending_entry else False,
                has_metadata=False,
            )
            await callback.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest:
            pass


# ============================================================
# Cover & Queue Handlers
# ============================================================

@router.callback_query(F.data == "keep_thumb")
async def on_keep_thumb(callback: CallbackQuery, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return

    uid = callback.from_user.id
    pending_entry = pending_audio.pop(uid, None)
    if not pending_entry:
        await callback.answer("Kutilayotgan audio topilmadi", show_alert=True)
        return

    audio = pending_entry["audio"]
    job_id = pending_entry["job_id"]

    try:
        await callback.message.edit_text(get_msg_job_queued(config.EMOJI_HOURGLASS))
    except TelegramBadRequest:
        pass
    await callback.answer()

    await start_job_worker(bot)
    job = {
        "message": pending_entry["message"],
        "audio": audio,
        "uid": uid,
        "job_id": job_id,
        "artist": pending_entry.get("artist"),
        "title": pending_entry.get("title"),
        "include_watermark": pending_entry.get("include_watermark", False),
    }
    tracked_jobs[job_id] = job
    user_pending_jobs.setdefault(uid, set()).add(job_id)
    await enqueue_job(job)


@router.callback_query(F.data == "change_thumb")
async def on_change_thumb(callback: CallbackQuery, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    pending_entry = pending_audio.get(uid)
    if not pending_entry:
        await callback.answer("Kutilayotgan audio topilmadi", show_alert=True)
        return

    pending_images[uid] = {
        "waiting_for_image": True,
        "audio_message_id": pending_entry["message"].message_id,
    }
    await callback.message.reply(get_msg_send_image_now(config.EMOJI_CAMERA))
    await callback.answer()


@router.callback_query(F.data == "add_image")
async def on_add_image(callback: CallbackQuery, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    pending_entry = pending_audio.get(uid)
    if not pending_entry:
        await callback.answer("Kutilayotgan audio topilmadi", show_alert=True)
        return
    pending_images[uid] = {
        "waiting_for_image": True,
        "audio_message_id": pending_entry["message"].message_id,
    }
    await callback.message.reply(get_msg_send_image_now(config.EMOJI_CAMERA))
    await callback.answer()


@router.message(F.photo)
async def on_photo_for_audio(message: Message, bot: Bot):
    if not message.from_user:
        return
    uid = message.from_user.id
    img_pending = pending_images.get(uid)
    if not img_pending or not img_pending.get("waiting_for_image"):
        return

    pending_entry = pending_audio.get(uid)
    if not pending_entry:
        pending_images.pop(uid, None)
        return

    photo = message.photo[-1]
    job = pending_entry
    job["thumbnail_file_id"] = photo.file_id
    job["message"] = pending_entry["message"]
    job["uid"] = uid
    job["job_id"] = pending_entry["job_id"]

    pending_audio.pop(uid, None)
    pending_images.pop(uid, None)

    await message.reply(get_msg_image_received(config.EMOJI_SUCCESS))

    tracked_jobs[job["job_id"]] = job
    user_pending_jobs.setdefault(job["uid"], set()).add(job["job_id"])

    await start_job_worker(bot)
    await enqueue_job(job)


@router.callback_query(F.data == "cancel_queue")
async def on_cancel_queue(callback: CallbackQuery, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    cancel_user_jobs(uid)
    pending_trim.pop(uid, None)
    pending_audio.pop(uid, None)
    pending_images.pop(uid, None)
    pending_batches.pop(uid, None)
    if uid in batch_debounce_tasks:
        batch_debounce_tasks[uid].cancel()
        batch_debounce_tasks.pop(uid, None)

    if callback.message:
        try:
            await callback.message.edit_text(get_msg_queue_canceled_edit(config.EMOJI_CANCEL))
        except TelegramBadRequest:
            pass
    await callback.answer(get_msg_queue_canceled_answer(config.EMOJI_SUCCESS))


@router.callback_query(F.data.startswith("vinyl:"))
async def on_vinyl_choice(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return
    raw_choice = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    if raw_choice in ("pink", "blue", "yellow"):
        user_vinyl_choice[uid] = raw_choice
        choice = raw_choice
    else:
        user_vinyl_choice.pop(uid, None)
        choice = "default"

    # UserPreference ga ham saqlash
    try:
        pref = await get_user_preference(session, uid)
        pref.preferred_vinyl = choice
        await session.commit()
    except Exception as e:
        logger.warning("Failed to save preferred_vinyl to DB: %s", e)

    try:
        await callback.message.edit_text(
            get_msg_vinyl_choice_saved_edit(choice, config.EMOJI_PALETTE),
            reply_markup=build_vinyl_keyboard(choice),
        )
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.warning("Edit message in on_vinyl_choice failed: %s", e)

    await callback.answer(get_msg_vinyl_choice_saved_answer(choice, config.EMOJI_SUCCESS))


@router.callback_query(F.data.startswith("speed:"))
async def on_speed_selected(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    if not callback.from_user:
        await callback.answer()
        return
    data = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    user_speed_choice[uid] = data

    try:
        pref = await get_user_preference(session, uid)
        pref.preferred_speed = data
        await session.commit()
    except Exception as e:
        logger.warning("Failed to save preferred_speed to DB: %s", e)

    try:
        await callback.message.edit_reply_markup(reply_markup=build_speed_keyboard(uid))
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.warning("Edit reply markup in on_speed_selected failed: %s", e)

    await callback.answer(get_msg_speed_saved_answer(config.EMOJI_SUCCESS))


@router.message(F.video | F.voice)
async def on_wrong_type(message: Message):
    await message.reply(get_msg_wrong_type(config.EMOJI_WARNING))

