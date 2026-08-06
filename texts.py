# -*- coding: utf-8 -*-
"""
Loyihada ishlatiladigan barcha matnlar (Telegram xabarlari, tugma matnlari,
log xabarlari) shu yerda o'zgaruvchi va funksiyalar sifatida to'plangan.
"""

def fmt_emoji(fallback: str, emoji_id: str | None = None) -> str:
    """Telegram HTML parse_mode formatingiz uchun custom emoji tegini hosil qiladi:
    <tg-emoji emoji-id="12345">fallback</tg-emoji>.

    Agar emoji_id bo'sh yoki None bo'lsa, oddiy fallback emojisini qaytaradi.
    """
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


# ============================================================
# config.py
# ============================================================
ERR_MISSING_BOT_TOKEN = "BOT_TOKEN muhit o'zgaruvchisiga (yoki .env fayliga) tokenni kiriting"


# ============================================================
# main.py
# ============================================================
LOG_BOT_RUNNING = "Bot ishga tushdi..."


# ============================================================
# handlers.py - holat animatsiyasi matnlari (StatusAnimator)
# ============================================================
STAGE_PREPARING = "Tayyorlanmoqda"
STAGE_DOWNLOADING_AUDIO = "Audio fayl yuklab olinmoqda"
STAGE_DOWNLOADING_THUMBNAIL = "Muqova rasmi yuklab olinmoqda"
STAGE_BUILDING_DISC = "Disk dizayni tuzilmoqda"
STAGE_RENDERING_VIDEO = "Video tayyorlanmoqda"
STAGE_UPLOADING_VIDEO = "Video yuklanmoqda va yuborilmoqda"


# ============================================================
# handlers.py - log xabarlari (logger)
# ============================================================
LOG_PROGRESS_UPDATE_FAILED = "Jarayon xabarini yangilab bo'lmadi"
LOG_DELETE_FAILED_FMT = "{p} faylini o'chirib bo'lmadi: {e}"
LOG_DOWNLOAD_RETRY_FAILED_FMT = "Faylni yuklab olish urinishi %s/%s muvaffaqiyatsiz bo'ldi: %s: %s"
LOG_NO_DETAIL_MESSAGE = "(batafsil xabar yo'q)"
LOG_QUEUE_PROCESS_FAILED = "Navbatdagi so'rovni qayta ishlashda xatolik yuz berdi"
LOG_PROCESS_JOB_FAILED = "So'rovni qayta ishlashda xatolik yuz berdi"
LOG_SEND_ERROR_FAILED = "Foydalanuvchiga xato xabarini yuborib bo'lmadi"
LOG_FILE_TOO_LARGE = "Fayl juda katta, baribir qayta ishlanadi va faqat birinchi bir daqiqasi olinadi"


# ============================================================
# handlers.py - ichki xatolar (Exceptions)
# ============================================================
ERR_NO_THUMBNAIL_AVAILABLE = "Muqova rasmi ham, zaxira rasm ham mavjud emas"
ERR_OUTPUT_NOT_CREATED = "Natijaviy video fayl yaratilmadi"


# ============================================================
# handlers.py - Telegram foydalanuvchiga ko'rinadigan xabarlar (Getters)
# ============================================================
def get_msg_audio_received(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⏳", emoji_id)
    return (
        f"{icon} Audio fayl qabul qilindi, hozir video ko'rinishga o'tkazilmoqda. "
        "Faylning faqat birinchi bir daqiqasi ishlatiladi."
    )

def get_msg_duration_too_long(duration: float, emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⚠️", emoji_id)
    return (
        f"{icon} Fayl ruxsat etilgandan uzunroq! {duration:.0f} soniya. "
        "Maksimal chegara — bir daqiqa. "
        "Sizga bir daqiqalik video yuboraman."
    )

def translate_error_to_uzbek(error_text: str) -> str:
    """Inglizcha texnik xatoliklarni va Telegram Bot API xatolarini o'zbek tiliga tushunarli tarzda tarjima qiladi."""
    err_lower = error_text.lower()

    if "file is too big" in err_lower or "file_too_big" in err_lower:
        return "Natijaviy video-xabar hajmi juda katta bo'lib ketdi (Telegram 12 MB me'yoridan oshib ketdi)."
    if "voice_messages_forbidden" in err_lower or "privacy" in err_lower or "user is privacy restricted" in err_lower:
        return "Sizning Telegram maxfiylik sozlamalaringizda video/ovozli xabarlarni qabul qilish taqiqlangan."
    if "bot was blocked" in err_lower or "user is deactivated" in err_lower:
        return "Bot foydalanuvchi tomonidan bloklangan."
    if "chat not found" in err_lower:
        return "Suhbat topilmadi."
    if "message to reaply not found" in err_lower or "message to edit not found" in err_lower:
        return "Tegishli xabar topilmadi yoki o'chirilgan."
    if "timeout" in err_lower or "timed out" in err_lower or "connectorerror" in err_lower:
        return "Telegram serveriga ulanishda vaqt tugadi (Internet tarmoq xatosi)."
    if "telegramservererror" in err_lower or "502 bad gateway" in err_lower or "500 internal" in err_lower or "503 service" in err_lower:
        return "Telegram serverlarida vaqtinchalik xatolik yuz berdi. Bir ozdan so'ng qayta urinib ko'ring."
    if "ffprobe" in err_lower or "ffmpeg" in err_lower:
        return "Audio/video faylni konvertatsiya qilishda (kodlashda) xatolik yuz berdi."
    if "no thumbnail" in err_lower or "err_no_thumbnail_available" in err_lower:
        return "Audio faylda muqova rasmi topilmadi."
    if "filenotfounderror" in err_lower or "err_output_not_created" in err_lower or "no such file or directory" in err_lower or "errno 2" in err_lower:
        return "Kerakli fayl topilmadi yoki Telegram serveridan yuklab bo'lmadi."
    if "download" in err_lower:
        return "Faylni Telegram serveridan yuklab bo'lmadi."

    return error_text


def get_msg_processing_error(error_text: str, emoji_id: str | None = None) -> str:
    icon = fmt_emoji("❌", emoji_id)
    translated = translate_error_to_uzbek(error_text)
    if translated != error_text:
        return f"{icon} Qayta ishlashda xatolik yuz berdi:\n<b>{translated}</b>"
    return f"{icon} Qayta ishlashda xatolik yuz berdi:\n<code>{error_text}</code>"


def get_msg_dev_choose_template(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("🎨", emoji_id)
    return f"{icon} <b>Vinyl plastinka rangini tanlang:</b>"

def get_msg_start_help(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("🎛️", emoji_id)
    return (
        "Menga muqova rasmi biriktirilgan audio fayl yuboring, "
        "men sizga o'sha rasm va ovoz bilan aylanuvchi disk (vinyl) videosini qaytaraman."
        f"\n\n{icon} Aylanish tezligini faqat vizual tarzda tanlang; "
        "bu ovoz yoki faylning tezligini o'zgartirmaydi:"
    )

def get_msg_template_files_missing(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⚠️", emoji_id)
    return (
        f"{icon} Shablon fayllari (vinyl.png / shadow.png) assets/ papkasida topilmadi. "
        "Ularni joylashtirib, qaytadan urinib ko'ring."
    )

def get_msg_no_thumbnail_prompt(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⚠️", emoji_id)
    return (
        f"{icon} Bu audio faylda muqova rasmi (thumbnail) yo'q. "
        "Rasm qo'shish uchun quyidagi tugmani bosishingiz mumkin, "
        "so'ng men uni audio fayl bilan birga ishlataman."
    )

def get_msg_job_queued(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("🧵", emoji_id)
    return (
        f"{icon} Fayl navbatga qo'shildi, oldingi fayllar tugagach qayta ishlanadi. "
        "Faylning faqat birinchi bir daqiqasi ishlatiladi."
    )

def get_msg_queue_canceled_edit(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("🗑️", emoji_id)
    return f"{icon} Ushbu foydalanuvchi uchun kutilayotgan ishlar bekor qilindi va navbat tozalandi."

def get_msg_queue_canceled_answer(emoji_id: str | None = None) -> str:
    return "✅ Kutilayotgan so'rovlar bekor qilindi"

def get_msg_send_image_now(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("📷", emoji_id)
    return f"{icon} Endi rasmni yuboring, men uni audio fayl bilan birga ishlataman."

def get_msg_no_pending_audio(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⚠️", emoji_id)
    return f"{icon} Bu rasmga bog'liq kutilayotgan audio fayl hali yo'q."

def get_msg_audio_expired(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⏰", emoji_id)
    return f"{icon} Audio faylni kutish muddati tugadi. Audio faylni qaytadan yuboring."

def get_msg_image_received(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("✅", emoji_id)
    return (
        f"{icon} Rasm qabul qilindi, bot endi audio fayl ustida ishlashni boshlaydi — "
        "uni qayta yuborish shart emas."
    )

def get_msg_vinyl_choice_saved_edit(choice: str = "default", emoji_id: str | None = None) -> str:
    icon = fmt_emoji("🎨", emoji_id)
    color_map = {
        "pink": "💗 Pushti",
        "yellow": "🟡 Sariq",
        "blue": "🔵 Ko'k",
        "default": "🖤 Oddiy (Klassik)",
    }
    selected = color_map.get(choice, "🖤 Oddiy (Klassik)")
    return f"{icon} Vinyl rangi tanlandi: <b>{selected}</b>"

def get_msg_vinyl_choice_saved_answer(choice: str = "default", emoji_id: str | None = None) -> str:
    color_map = {
        "pink": "💗 Pushti",
        "yellow": "🟡 Sariq",
        "blue": "🔵 Ko'k",
        "default": "🖤 Oddiy",
    }
    selected = color_map.get(choice, "🖤 Oddiy")
    return f"✅ {selected} rang saqlandi"

def get_msg_speed_saved_answer(emoji_id: str | None = None) -> str:
    return "✅ Ushbu foydalanuvchi uchun disk tezligi saqlandi"

def get_msg_wrong_type(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("📌", emoji_id)
    return f"{icon} Audio fayl (Audio) yuboring, video yoki hujjat emas — shundagina uning muqova rasmi mavjud bo'ladi."


# ============================================================
# handlers.py - Thumbnail o'zgartirish taklifi xabarlari
# ============================================================
def get_msg_change_thumbnail_prompt(emoji_id_camera: str | None = None, emoji_id_music: str | None = None) -> str:
    cam = fmt_emoji("🎵", emoji_id_music)
    return (
        f"{cam} Audio fayl qabul qilindi!\n\n"
        "Bu audioda allaqachon muqova rasmi bor. "
        "<b>Rasmni o'zgartirasizmi?</b>\n\n"
        "• Yangi rasm tashlamoqchi bo'lsangiz — <b>Ha, o'zgartiraman</b> tugmasini bosing\n"
        "• Mavjud rasm bilan davom etmoqchi bo'lsangiz — <b>Davom etish</b> tugmasini bosing"
    )

def get_btn_change_thumbnail_yes(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Ha, o'zgartiraman"
    return "🖼 Ha, o'zgartiraman"

def get_btn_keep_thumbnail(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Davom etish"
    return "▶️ Davom etish"

# Standart konstantalar (orqaga moslik uchun)
MSG_AUDIO_RECEIVED = get_msg_audio_received()
MSG_DURATION_TOO_LONG_FMT = get_msg_duration_too_long(60)
MSG_PROCESSING_ERROR_FMT = get_msg_processing_error("{error_text}")
MSG_DEV_CHOOSE_TEMPLATE = get_msg_dev_choose_template()
MSG_START_HELP = get_msg_start_help()
MSG_TEMPLATE_FILES_MISSING = get_msg_template_files_missing()
MSG_NO_THUMBNAIL_PROMPT = get_msg_no_thumbnail_prompt()
MSG_JOB_QUEUED = get_msg_job_queued()
MSG_QUEUE_CANCELED_EDIT = get_msg_queue_canceled_edit()
MSG_QUEUE_CANCELED_ANSWER = get_msg_queue_canceled_answer()
MSG_SEND_IMAGE_NOW = get_msg_send_image_now()
MSG_NO_PENDING_AUDIO = get_msg_no_pending_audio()
MSG_AUDIO_EXPIRED = get_msg_audio_expired()
MSG_IMAGE_RECEIVED = get_msg_image_received()
MSG_DEV_ONLY_OPTION = "Bu variant faqat dasturchi uchun"
MSG_VINYL_CHOICE_SAVED_EDIT = get_msg_vinyl_choice_saved_edit()
MSG_VINYL_CHOICE_SAVED_ANSWER = get_msg_vinyl_choice_saved_answer()
MSG_SPEED_SAVED_ANSWER = get_msg_speed_saved_answer()
MSG_WRONG_TYPE = get_msg_wrong_type()


# ============================================================
# handlers.py - Audio kesish (trim) xabarlari
# ============================================================
def get_msg_trim_prompt(duration: float, emoji_id: str | None = None) -> str:
    icon = fmt_emoji("✂️", emoji_id)
    return (
        f"{icon} Audio fayl <b>{int(duration)}</b> soniya uzunligida. "
        "Maksimal chegara — 60 soniya.\n\n"
        "Kesmoqchi bo'lsangiz <code>boshlanish:tugash</code> formatida yozing "
        "(masalan <code>10:100</code> — 10‑soniyadan boshlab 60 soniya olinadi).\n\n"
        "Yoki <b>Davom etish</b> tugmasini bosing — birinchi 60 soniyasi olinadi."
    )

def get_msg_trim_accepted(start: int, end: int, emoji_id: str | None = None) -> str:
    icon = fmt_emoji("✅", emoji_id)
    return f"{icon} Audio <b>{start}–{end}</b> soniya oralig'ida kesiladi ({end - start} soniya)."

def get_msg_trim_invalid(emoji_id: str | None = None) -> str:
    icon = fmt_emoji("⚠️", emoji_id)
    return (
        f"{icon} Noto'g'ri format. <code>boshlanish:tugash</code> formatida yozing "
        "(masalan <code>10:100</code>)."
    )

def get_btn_continue_no_trim(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Davom etish"
    return "▶️ Davom etish"

BTN_CONTINUE_NO_TRIM = get_btn_continue_no_trim()

# ============================================================
# handlers.py - tugma matnlari (Inline Keyboard buttons Getters & Constants)
# ============================================================
def get_btn_add_image(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Rasm qo'shish"
    return "➕ Rasm qo'shish"

def get_btn_cancel(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Bekor qilish"
    return "❌ Bekor qilish"

def get_btn_vinyl_pink(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Pushti rangdan foydalanish"
    return "💗 Pushti rangdan foydalanish"

def get_btn_vinyl_default(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Oddiysidan foydalanish"
    return "🔙 Oddiysidan foydalanish"

def get_btn_vinyl_yellow(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Sariq rang"
    return "🟡 Sariq rang"

def get_btn_vinyl_blue(emoji_id: str | None = None) -> str:
    if emoji_id:
        return "Ko'k rang"
    return "🔵 Ko'k rang"

def get_speed_label_full(emoji_id: str | None = None) -> str:
    if not emoji_id:
        return "🔄 To'liq aylanish"
    return "To'liq aylanish"

def get_speed_label_8rpm(emoji_id: str | None = None) -> str:
    if not emoji_id:
        return "⚡ 8 RPM"
    return "8 RPM"

def get_speed_label_33rpm(emoji_id: str | None = None) -> str:
    if not emoji_id:
        return "⚡ 33 RPM"
    return "33 RPM"

def get_speed_label_45rpm(emoji_id: str | None = None) -> str:
    if not emoji_id:
        return "⚡ 45 RPM"
    return "45 RPM"

# Standart konstantalar (orqaga moslik uchun)
BTN_ADD_IMAGE = get_btn_add_image()
BTN_CANCEL = get_btn_cancel()

BTN_VINYL_PINK = get_btn_vinyl_pink()
BTN_VINYL_DEFAULT = get_btn_vinyl_default()
BTN_VINYL_YELLOW = get_btn_vinyl_yellow()
BTN_VINYL_BLUE = get_btn_vinyl_blue()

SPEED_LABEL_FULL = get_speed_label_full()
SPEED_LABEL_8RPM = get_speed_label_8rpm()
SPEED_LABEL_33RPM = get_speed_label_33rpm()
SPEED_LABEL_45RPM = get_speed_label_45rpm()


