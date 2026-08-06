import asyncio
import logging
import os
import re
import time
import uuid

from aiogram import Router, F, Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import Channel
from sub_service import check_subscriptions
from states import AddChannelState
from compose import build_disc
from processor import get_duration, render_vinyl, extract_embedded_cover
import config
from texts import (
    fmt_emoji,
    STAGE_PREPARING,
    STAGE_DOWNLOADING_AUDIO,
    STAGE_DOWNLOADING_THUMBNAIL,
    STAGE_BUILDING_DISC,
    STAGE_RENDERING_VIDEO,
    STAGE_UPLOADING_VIDEO,
    LOG_PROGRESS_UPDATE_FAILED,
    LOG_DELETE_FAILED_FMT,
    LOG_DOWNLOAD_RETRY_FAILED_FMT,
    LOG_NO_DETAIL_MESSAGE,
    LOG_QUEUE_PROCESS_FAILED,
    LOG_PROCESS_JOB_FAILED,
    LOG_SEND_ERROR_FAILED,
    LOG_FILE_TOO_LARGE,
    ERR_NO_THUMBNAIL_AVAILABLE,
    ERR_OUTPUT_NOT_CREATED,
    get_msg_audio_received,
    get_msg_duration_too_long,
    get_msg_processing_error,
    get_msg_dev_choose_template,
    get_msg_start_help,
    get_msg_template_files_missing,
    get_msg_no_thumbnail_prompt,
    get_msg_job_queued,
    get_msg_queue_canceled_edit,
    get_msg_queue_canceled_answer,
    get_msg_send_image_now,
    get_msg_no_pending_audio,
    get_msg_audio_expired,
    get_msg_image_received,
    MSG_DEV_ONLY_OPTION,
    get_msg_vinyl_choice_saved_edit,
    get_msg_vinyl_choice_saved_answer,
    get_msg_speed_saved_answer,
    get_msg_wrong_type,
    get_msg_trim_prompt,
    get_msg_trim_accepted,
    get_msg_trim_invalid,
    get_btn_continue_no_trim,
    BTN_ADD_IMAGE,
    BTN_CANCEL,
    BTN_VINYL_PINK,
    BTN_VINYL_DEFAULT,
    BTN_VINYL_YELLOW,
    BTN_VINYL_BLUE,
    SPEED_LABEL_FULL,
    SPEED_LABEL_8RPM,
    SPEED_LABEL_33RPM,
    SPEED_LABEL_45RPM,
    get_btn_add_image,
    get_btn_cancel,
    get_btn_vinyl_pink,
    get_btn_vinyl_default,
    get_btn_vinyl_yellow,
    get_btn_vinyl_blue,
    get_speed_label_full,
    get_speed_label_8rpm,
    get_speed_label_33rpm,
    get_speed_label_45rpm,
    get_msg_change_thumbnail_prompt,
    get_btn_change_thumbnail_yes,
    get_btn_keep_thumbnail,
)

logger = logging.getLogger(__name__)
router = Router()

job_queue: asyncio.Queue[dict] = asyncio.Queue()
developer_job_queue: asyncio.Queue[dict] = asyncio.Queue()
worker_tasks: list[asyncio.Task] = []
pending_images: dict[int, dict] = {}
pending_audio: dict[int, dict] = {}
user_speed_choice: dict[int, str] = {}
user_rotation_seconds: dict[int, float | None] = {}
user_pending_jobs: dict[int, set[str]] = {}
tracked_jobs: dict[str, dict] = {}
canceled_job_ids: set[str] = set()
user_vinyl_choice: dict[int, str] = {}
pending_trim: dict[int, dict] = {}


HOURGLASS_FRAMES = ["⏳", "⌛"]
PROGRESS_BAR_WIDTH = 12
STATUS_UPDATE_INTERVAL_SECONDS = 2.2


def render_progress_bar(percent: float, width: int = PROGRESS_BAR_WIDTH) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100))
    return "▓" * filled + "░" * (width - filled)


class StatusAnimator:
    """Telegramdagi holat xabarini davriy yangilaydi: harakatlanuvchi qum soat + matn/progress bar."""

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
                await asyncio.wait_for(self._stop_event.wait(), timeout=STATUS_UPDATE_INTERVAL_SECONDS)
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
    p = re.sub(r'^.*?\d+:[^/]+/', '', p)
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


async def download_with_retries(bot: Bot, file_id: str, destination: str,
                                timeout_seconds: int, retries: int = 3) -> None:
    # 1. get_file orqali fayl ma'lumotlarini olish
    local_path = ""
    try:
        file_info = await bot.get_file(file_id)
        local_path = file_info.file_path or ""
    except Exception as e:
        logger.warning("bot.get_file(%s) failed: %s", file_id, e)

    # 2. Local diskda mavjud bo'lsa — darhol nusxalaymiz
    if local_path and os.path.exists(local_path):
        try:
            import shutil as _shutil
            _shutil.copy2(local_path, destination)
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return
        except Exception as copy_err:
            logger.warning("Local file copy failed: %s", copy_err)

    rel_path = extract_rel_path(local_path)

    # 3. Local Bot API HTTP orqali yuklab olishga urinish
    if rel_path and config.TELEGRAM_LOCAL_API_URL:
        local_http_url = f"{config.TELEGRAM_LOCAL_API_URL.rstrip('/')}/file/bot{config.BOT_TOKEN}/{rel_path}"
        try:
            await download_file_http(local_http_url, destination, timeout_seconds=timeout_seconds)
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return
        except Exception as http_err:
            logger.warning("Local API HTTP download failed (%s), trying official API fallback...", http_err)

    # 4. Rasmiy Telegram HTTPS serveridan yuklab olish
    if rel_path:
        official_url = f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{rel_path}"
        try:
            await download_file_http(official_url, destination, timeout_seconds=timeout_seconds)
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return
        except Exception as off_err:
            logger.warning("Official API fallback download failed: %s", off_err)

    # 5. Standart bot.download() va qayta urinishlar sikli
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
                attempt, retries, type(exc).__name__, exc or LOG_NO_DETAIL_MESSAGE,
            )
            if attempt < retries:
                await asyncio.sleep(2)
            else:
                raise
    if last_error is not None:
        raise last_error


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
        try:
            if job_id in canceled_job_ids:
                canceled_job_ids.discard(job_id)
                tracked_jobs.pop(job_id, None)
                user_pending_jobs.get(job.get("uid", 0), set()).discard(job_id)
                continue

            tracked_jobs[job_id] = job
            await process_job(bot, job)
        except Exception:
            logger.exception(LOG_QUEUE_PROCESS_FAILED)
        finally:
            tracked_jobs.pop(job_id, None)
            user_pending_jobs.get(job.get("uid", 0), set()).discard(job_id)
            if queue is not None:
                queue.task_done()


async def start_job_worker(bot: Bot) -> None:
    """MAX_CONCURRENT_JOBS ga mos sonda ishchi (worker) task ishga tushiradi.

    Oldingi versiyada faqat bitta worker ishlab, so'rovlar navbatda ketma-ket
    (bir vaqtda bittadan) qayta ishlanardi — MAX_CONCURRENT_JOBS sozlamasi
    e'tiborga olinmasdan qolib ketardi. Endi tugagan tasklar tozalanib,
    yetishmagan sondagi yangi workerlar qo'shiladi, shu bilan bir nechta
    fayl haqiqatan ham parallel qayta ishlanadi.
    """
    worker_tasks[:] = [t for t in worker_tasks if not t.done()]
    target = max(1, config.MAX_CONCURRENT_JOBS)
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


def get_developer_vinyl_path(user_id: int) -> str:
    choice = user_vinyl_choice.get(user_id)
    if choice == "pink":
        return config.VINYL_PINK_PATH
    if choice == "yellow":
        return config.VINYL_YELLOW_PATH
    if choice == "blue":
        return config.VINYL_BLUE_PATH
    return config.VINYL_PATH


def get_developer_shadow_path(user_id: int) -> str:
    choice = user_vinyl_choice.get(user_id)
    if choice == "pink":
        return config.SHADOW_PINK_PATH
    if choice == "yellow":
        return config.SHADOW_YELLOW_PATH
    if choice == "blue":
        return config.SHADOW_BLUE_PATH
    return config.SHADOW_PATH


def get_job_priority(user_id: int) -> int:
    return 0 if user_id and user_id == config.DEVELOPER_ID else 1


def enqueue_job(job: dict) -> None:
    if get_job_priority(job.get("uid", 0)) == 0:
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
    # Xotira to'lib ketmasligi uchun eski yozuvlarni tozalab turish
    _MAX_CANCELED_IDS = 500
    if len(canceled_job_ids) > _MAX_CANCELED_IDS:
        # Eng qadimiy yozuvlarni olib tashlash (set tartibsiz, shuning uchun bir qismini o'chiramiz)
        overflow = len(canceled_job_ids) - _MAX_CANCELED_IDS
        for old_id in list(canceled_job_ids)[:overflow]:
            canceled_job_ids.discard(old_id)


async def process_job(bot: Bot, job: dict) -> None:
    message = job["message"]
    audio = job["audio"]
    uid = job["uid"]
    job_id = job["job_id"]

    fname = getattr(audio, "file_name", None) or ""
    ext = fname.rsplit(".", 1)[-1] if "." in fname else "mp3"
    audio_path = tmp(f"{uid}_{job_id}_audio.{ext}")
    thumb_path = tmp(f"{uid}_{job_id}_thumb.jpg")
    disc_path = tmp(f"{uid}_{job_id}_disc.png")
    out_path = tmp(f"{uid}_{job_id}_out.mp4")
    job["temp_paths"] = [audio_path, thumb_path, disc_path, out_path]

    status = await message.reply(get_msg_audio_received(config.EMOJI_HOURGLASS))
    animator = StatusAnimator(status)
    animator.start()

    try:
        await bot.send_chat_action(message.chat.id, action=ChatAction.RECORD_VIDEO_NOTE)
        animator.set_stage(STAGE_DOWNLOADING_AUDIO)
        await download_with_retries(bot, audio.file_id, audio_path, timeout_seconds=300, retries=3)

        thumbnail_file_id = None
        if job.get("thumbnail_file_id"):
            # Foydalanuvchi o'zi yuborgan rasm — ustunlik beradi
            thumbnail_file_id = job["thumbnail_file_id"]
        else:
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
            # Faylning o'zidan ichki muqova rasmini (embedded cover art/ID3 tag) ajratib olishga harakat qilamiz
            animator.set_stage(STAGE_DOWNLOADING_THUMBNAIL)
            extracted = await extract_embedded_cover(audio_path, thumb_path)
            if extracted:
                thumb_obtained = True

        if not thumb_obtained:
            # Thumbnail mutlaqo yo'q (Telegram metadata-da ham, MP3 fayl ichida ham rasm yo'q)
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

            await message.reply(
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
            }
            pending_images[uid] = {"waiting_for_image": True, "audio_message_id": message.message_id}
            cleanup(audio_path, thumb_path, disc_path, out_path)
            return

        duration = await get_duration(audio_path)
        start_offset = job.get("start_offset", 0.0)
        if not job.get("trim_handled") and duration > config.MAX_DURATION_SECONDS:
            await message.reply(get_msg_duration_too_long(duration, config.EMOJI_WARNING))

        await bot.send_chat_action(message.chat.id, action=ChatAction.UPLOAD_VIDEO_NOTE)
        animator.set_stage(STAGE_BUILDING_DISC)
        await asyncio.to_thread(
            build_disc, thumb_path, get_developer_vinyl_path(uid), disc_path,
            config.HOLE_RATIO, config.DISC_SIZE,
        )

        animator.set_stage(STAGE_RENDERING_VIDEO, percent=0)

        async def on_render_progress(percent: float) -> None:
            animator.set_stage(STAGE_RENDERING_VIDEO, percent=percent)

        await render_vinyl(
            disc_path, get_developer_shadow_path(uid), audio_path, out_path,
            rotation_seconds=get_user_rotation_seconds(uid),
            size=config.DISC_SIZE, fps=config.OUTPUT_FPS,
            max_duration=config.MAX_DURATION_SECONDS,
            start_offset=start_offset,
            on_progress=on_render_progress,
        )
        if not os.path.exists(out_path):
            raise FileNotFoundError(ERR_OUTPUT_NOT_CREATED)

        animator.set_stage(STAGE_UPLOADING_VIDEO, percent=100)
        await bot.send_chat_action(message.chat.id, action=ChatAction.UPLOAD_VIDEO_NOTE)
        await message.reply_video_note(FSInputFile(out_path), length=config.DISC_SIZE)
    except Exception as e:
        logger.exception(LOG_PROCESS_JOB_FAILED)
        error_text = str(e) or repr(e) or e.__class__.__name__
        try:
            await message.reply(get_msg_processing_error(error_text, config.EMOJI_ERROR))
        except Exception:
            logger.exception(LOG_SEND_ERROR_FAILED)
    finally:
        await animator.stop()
        cleanup(audio_path, thumb_path, disc_path, out_path)
        try:
            await status.delete()
        except Exception:
            pass


def build_speed_keyboard(user_id: int) -> InlineKeyboardMarkup:
    current_key = get_user_speed_key(user_id)
    has_speed_emoji = bool(config.BTN_EMOJI_SPEED_ACTIVE or config.BTN_EMOJI_SPEED_INACTIVE or config.BTN_EMOJI_SPEED)
    labels = [
        (get_speed_label_full("yes" if has_speed_emoji else None), "full"),
        (get_speed_label_8rpm("yes" if has_speed_emoji else None), "8"),
        (get_speed_label_33rpm("yes" if has_speed_emoji else None), "33"),
        (get_speed_label_45rpm("yes" if has_speed_emoji else None), "45"),
    ]
    buttons = []
    for label, value in labels:
        selected = (current_key == value)
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


@router.message(F.text == "/dev")
async def on_dev(message: Message):
    if not message.from_user or message.from_user.id != config.DEVELOPER_ID:
        return
    current = user_vinyl_choice.get(message.from_user.id, "default")
    await message.reply(
        get_msg_dev_choose_template(config.EMOJI_PALETTE),
        reply_markup=build_vinyl_keyboard(current)
    )


@router.message(F.text.in_({"/start", "/help"}))
async def on_start(message: Message):
    await message.reply(
        get_msg_start_help(config.EMOJI_SPEED),
        reply_markup=build_speed_keyboard(message.from_user.id if message.from_user else 0),
    )


def build_vinyl_keyboard(selected_choice: str | None = None) -> InlineKeyboardMarkup:
    """Barcha foydalanuvchilar uchun vinyl rang tanlash klaviaturasi."""
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
        check_mark = "✅ " if (selected and not emoji_id) else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{check_mark}{text}",
                callback_data=f"vinyl:{choice_key}",
                style=style,
                icon_custom_emoji_id=emoji_id or None,
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "/rang")
async def on_rang(message: Message):
    """Barcha foydalanuvchilar uchun vinyl rang tanlash."""
    if not message.from_user:
        return
    current = user_vinyl_choice.get(message.from_user.id, "default")
    await message.reply(
        get_msg_dev_choose_template(config.EMOJI_PALETTE),
        reply_markup=build_vinyl_keyboard(current)
    )


@router.callback_query(F.data == "check_sub")
async def on_check_sub(callback: CallbackQuery, bot: Bot, session: AsyncSession):
    """Foydalanuvchi '✅ A'zo bo'ldim' tugmasini bosganda qayta tekshiradi."""
    if not callback.from_user:
        await callback.answer()
        return

    user_id = callback.from_user.id
    unsubscribed = await check_subscriptions(bot=bot, user_id=user_id, session=session)

    if unsubscribed:
        await callback.answer(
            "❌ Hali barcha kanallarga a'zo bo'lmadingiz!",
            show_alert=True,
        )
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


# ============================================================
# ADMIN PANEL: Majburiy kanallarni boshqarish
# ============================================================

@router.message(F.text == "/channels")
async def show_channels_admin(message: Message, session: AsyncSession):
    """Admin uchun majburiy kanallar ro'yxatini ko'rsatish."""
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
    """Admin yangi kanal qo'shish tugmasini bosganda FSM ga kirish."""
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
    """Admin yuborgan kanal ma'lumotlarini qabul qilib bazaga saqlash."""
    if not message.from_user or message.from_user.id != config.DEVELOPER_ID:
        return

    text = (message.text or "").strip()
    if " - " not in text:
        await message.reply(
            "❌ Noto'g'ri format! Iltimos, <code>Kanal Nomi - https://t.me/link</code> shaklida yuboring."
        )
        return

    name, url = text.split(" - ", 1)
    name = name.strip()
    url = url.strip()

    new_channel = Channel(name=name, url=url)
    session.add(new_channel)
    await session.commit()

    await state.clear()
    await message.reply(f"✅ <b>'{name}'</b> kanali muvaffaqiyatli bazaga qo'shildi!")


@router.callback_query(F.data.startswith("del_ch_"))
async def delete_channel_callback(callback: CallbackQuery, session: AsyncSession):
    """Kanalni bazadan o'chirish handler."""
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



@router.message(F.audio | F.document)
async def on_audio(message: Message, bot: Bot):
    if not message.from_user:
        return

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

    uid = message.from_user.id
    job_id = uuid.uuid4().hex

    # Agar audio 60 soniyadan uzun bo'lsa, kesish taklif qilinadi
    duration = getattr(audio, "duration", 0) or 0
    if duration > config.MAX_DURATION_SECONDS:
        pending_trim[uid] = {
            "audio": audio,
            "message": message,
            "duration": duration,
            "job_id": job_id,
            "uid": uid,
            "expires_at": time.time() + 300,
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=get_btn_continue_no_trim(config.BTN_EMOJI_CONTINUE),
                callback_data="trim_continue",
                style="success",
                icon_custom_emoji_id=config.BTN_EMOJI_CONTINUE or None,
            )],
            [InlineKeyboardButton(
                text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                callback_data="cancel_queue",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            )],
        ])
        await message.reply(
            get_msg_trim_prompt(duration, config.EMOJI_TRIM),
            reply_markup=keyboard,
        )
        return

    # Audio thumbnail borligi tekshiriladi (Telegram thumbnail yoki fayl ichidagi rasm bilan davom etish tanlovi)
    has_thumb = bool(getattr(audio, "thumbnail", None) or getattr(audio, "thumb", None))

    if has_thumb:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=get_btn_change_thumbnail_yes(config.BTN_EMOJI_ADD_IMAGE),
                callback_data="change_thumb",
                style="primary",
                icon_custom_emoji_id=config.BTN_EMOJI_ADD_IMAGE or None,
            )],
            [InlineKeyboardButton(
                text=get_btn_keep_thumbnail(config.BTN_EMOJI_CONTINUE),
                callback_data="keep_thumb",
                style="success",
                icon_custom_emoji_id=config.BTN_EMOJI_CONTINUE or None,
            )],
            [InlineKeyboardButton(
                text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                callback_data="cancel_queue",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            )],
        ])
        prompt_msg = get_msg_change_thumbnail_prompt(
            emoji_id_camera=config.EMOJI_CAMERA,
            emoji_id_music=config.EMOJI_MUSIC,
        )
    else:
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
        prompt_msg = get_msg_no_thumbnail_prompt(config.EMOJI_WARNING)

    await message.reply(prompt_msg, reply_markup=keyboard)

    pending_audio[uid] = {
        "audio": audio,
        "message": message,
        "expires_at": time.time() + 300,
        "job_id": job_id,
        "uid": uid,
        "has_thumbnail": has_thumb,
    }
    pending_images[uid] = {"audio_message_id": message.message_id}


@router.callback_query(F.data == "cancel_queue")
async def on_cancel_queue(callback, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    cancel_user_jobs(callback.from_user.id)
    pending_trim.pop(callback.from_user.id, None)
    pending_audio.pop(callback.from_user.id, None)
    pending_images.pop(callback.from_user.id, None)
    if callback.message:
        try:
            await callback.message.edit_text(get_msg_queue_canceled_edit(config.EMOJI_CANCEL))
        except TelegramBadRequest:
            pass
    await callback.answer(get_msg_queue_canceled_answer(config.EMOJI_SUCCESS))


@router.callback_query(F.data == "add_image")
async def on_add_image(callback, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    pending_entry = pending_audio.get(uid)
    if not pending_entry:
        await callback.answer("Kutilayotgan audio topilmadi", show_alert=True)
        return
    pending_images[uid] = {"waiting_for_image": True, "audio_message_id": pending_entry["message"].message_id}
    await callback.message.reply(get_msg_send_image_now(config.EMOJI_CAMERA))
    await callback.answer()


@router.callback_query(F.data == "keep_thumb")
async def on_keep_thumb(callback, bot: Bot):
    """Foydalanuvchi mavjud thumbnail bilan davom etishni tanladi."""
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

    await callback.message.edit_text(get_msg_job_queued(config.EMOJI_HOURGLASS))
    await callback.answer()

    # Trim tekshiruvi
    audio_dur = getattr(audio, "duration", 0) or 0
    if audio_dur > config.MAX_DURATION_SECONDS:
        pending_trim[uid] = {
            "audio": audio,
            "message": pending_entry["message"],
            "duration": audio_dur,
            "job_id": job_id,
            "uid": uid,
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=get_btn_continue_no_trim(config.BTN_EMOJI_CONTINUE),
                callback_data="trim_continue",
                style="success",
                icon_custom_emoji_id=config.BTN_EMOJI_CONTINUE or None,
            )],
            [InlineKeyboardButton(
                text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                callback_data="cancel_queue",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            )],
        ])
        await pending_entry["message"].reply(
            get_msg_trim_prompt(audio_dur, config.EMOJI_TRIM),
            reply_markup=keyboard,
        )
        return

    await start_job_worker(bot)
    job = {
        "message": pending_entry["message"],
        "audio": audio,
        "uid": uid,
        "job_id": job_id,
    }
    tracked_jobs[job_id] = job
    user_pending_jobs.setdefault(uid, set()).add(job_id)
    enqueue_job(job)


@router.callback_query(F.data == "change_thumb")
async def on_change_thumb(callback, bot: Bot):
    """Foydalanuvchi yangi thumbnail yubormoqchi."""
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    pending_entry = pending_audio.get(uid)
    if not pending_entry:
        await callback.answer("Kutilayotgan audio topilmadi", show_alert=True)
        return

    # waiting_for_image rejimiga o'tkazamiz
    pending_images[uid] = {"waiting_for_image": True, "audio_message_id": pending_entry["message"].message_id}
    await callback.message.reply(get_msg_send_image_now(config.EMOJI_CAMERA))
    await callback.answer()


@router.message(F.photo)
async def on_photo_for_audio(message: Message, bot: Bot):
    if not message.from_user:
        return

    uid = message.from_user.id

    # Faqat waiting_for_image rejimida bo'lgandagina qayta ishlaymiz
    img_pending = pending_images.get(uid)
    if not img_pending or not img_pending.get("waiting_for_image"):
        return

    pending_entry = pending_audio.get(uid)
    if not pending_entry:
        # Audio yo'q — rasmni e'tiborsiz qoldiramiz
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

    # Agar audio 60 soniyadan uzun — kesish taklif qilish
    audio_dur = getattr(job["audio"], "duration", 0) or 0
    if audio_dur > config.MAX_DURATION_SECONDS:
        pending_trim[uid] = {
            "audio": job["audio"],
            "message": job["message"],
            "duration": audio_dur,
            "job_id": job["job_id"],
            "uid": job["uid"],
            "thumbnail_file_id": job.get("thumbnail_file_id"),
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=get_btn_continue_no_trim(config.BTN_EMOJI_CONTINUE),
                callback_data="trim_continue",
                style="success",
                icon_custom_emoji_id=config.BTN_EMOJI_CONTINUE or None,
            )],
            [InlineKeyboardButton(
                text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
                callback_data="cancel_queue",
                style="danger",
                icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
            )],
        ])
        await message.reply(
            get_msg_trim_prompt(audio_dur, config.EMOJI_TRIM),
            reply_markup=keyboard,
        )
        return

    tracked_jobs[job["job_id"]] = job
    user_pending_jobs.setdefault(job["uid"], set()).add(job["job_id"])

    await start_job_worker(bot)
    enqueue_job(job)
    return


@router.callback_query(F.data.startswith("vinyl:"))
async def on_vinyl_choice(callback, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    raw_choice = callback.data.split(":", 1)[1]
    if raw_choice in ("pink", "blue", "yellow"):
        user_vinyl_choice[callback.from_user.id] = raw_choice
        choice = raw_choice
    else:
        user_vinyl_choice.pop(callback.from_user.id, None)
        choice = "default"

    await callback.message.edit_text(
        get_msg_vinyl_choice_saved_edit(choice, config.EMOJI_PALETTE),
        reply_markup=build_vinyl_keyboard(choice)
    )
    await callback.answer(get_msg_vinyl_choice_saved_answer(choice, config.EMOJI_SUCCESS))


@router.callback_query(F.data.startswith("speed:"))
async def on_speed_selected(callback, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    data = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    user_speed_choice[user_id] = data
    try:
        await callback.message.edit_reply_markup(reply_markup=build_speed_keyboard(user_id))
    except TelegramBadRequest:
        pass
    await callback.answer(get_msg_speed_saved_answer(config.EMOJI_SUCCESS))


@router.callback_query(F.data == "trim_continue")
async def on_trim_continue(callback, bot: Bot):
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    trim_data = pending_trim.pop(uid, None)
    if not trim_data:
        await callback.answer("Kutilayotgan audio topilmadi")
        return

    await start_job_worker(bot)
    job = {
        "message": trim_data["message"],
        "audio": trim_data["audio"],
        "uid": uid,
        "job_id": trim_data["job_id"],
        "trim_handled": True,
        "start_offset": 0.0,
    }
    if trim_data.get("thumbnail_file_id"):
        job["thumbnail_file_id"] = trim_data["thumbnail_file_id"]
    tracked_jobs[job["job_id"]] = job
    user_pending_jobs.setdefault(uid, set()).add(job["job_id"])
    enqueue_job(job)

    await callback.message.edit_text(get_msg_job_queued(config.EMOJI_HOURGLASS))
    await callback.answer()


@router.message(F.text)
async def on_trim_text(message: Message, bot: Bot):
    """Foydalanuvchi boshlanish:tugash formatida audio kesish oralig'ini yuboradi."""
    if not message.from_user:
        return
    uid = message.from_user.id
    trim_data = pending_trim.get(uid)
    if not trim_data:
        return  # Kutilayotgan kesish yo'q — e'tiborsiz qoldiriladi

    text = (message.text or "").strip()
    match = re.match(r'^(\d+)\s*:\s*(\d+)$', text)
    if not match:
        await message.reply(get_msg_trim_invalid(config.EMOJI_WARNING))
        return

    start = int(match.group(1))
    end = int(match.group(2))
    audio_dur = trim_data["duration"]

    if start < 0 or end <= start or start >= audio_dur:
        await message.reply(get_msg_trim_invalid(config.EMOJI_WARNING))
        return

    # Tugash nuqtasini start + 60 bilan chegaralash
    actual_end = min(end, start + int(config.MAX_DURATION_SECONDS))
    actual_end = min(actual_end, audio_dur)
    actual_duration = actual_end - start

    if actual_duration <= 0:
        await message.reply(get_msg_trim_invalid(config.EMOJI_WARNING))
        return

    pending_trim.pop(uid)

    await message.reply(get_msg_trim_accepted(start, actual_end, config.EMOJI_SUCCESS))

    await start_job_worker(bot)
    job = {
        "message": trim_data["message"],
        "audio": trim_data["audio"],
        "uid": uid,
        "job_id": trim_data["job_id"],
        "trim_handled": True,
        "start_offset": float(start),
    }
    if trim_data.get("thumbnail_file_id"):
        job["thumbnail_file_id"] = trim_data["thumbnail_file_id"]
    tracked_jobs[job["job_id"]] = job
    user_pending_jobs.setdefault(uid, set()).add(job["job_id"])
    enqueue_job(job)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=get_btn_cancel(config.BTN_EMOJI_CANCEL),
            callback_data="cancel_queue",
            style="danger",
            icon_custom_emoji_id=config.BTN_EMOJI_CANCEL or None,
        )
    ]])
    await message.reply(
        get_msg_job_queued(config.EMOJI_HOURGLASS),
        reply_markup=keyboard,
    )


@router.message(F.video | F.voice)
async def on_wrong_type(message: Message):
    await message.reply(get_msg_wrong_type(config.EMOJI_WARNING))
