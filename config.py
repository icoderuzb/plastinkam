import os
from dotenv import load_dotenv

from texts import ERR_MISSING_BOT_TOKEN

load_dotenv()  # .env fayli mavjud bo'lsa, uni avtomatik o'qiydi

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError(ERR_MISSING_BOT_TOKEN)

MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", 3)))
DEVELOPER_ID = int(os.environ.get("DEVELOPER_ID", 0))
ROTATION_SECONDS = float(os.environ.get("ROTATION_SECONDS", 4))
OUTPUT_FPS = int(os.environ.get("OUTPUT_FPS", 30))
DISC_SIZE = int(os.environ.get("DISC_SIZE", 640))
HOLE_RATIO = float(os.environ.get("HOLE_RATIO", 0.42))

MAX_DURATION_SECONDS = float(os.environ.get("MAX_DURATION_SECONDS", 60))  # Telegram video-note chegarasi
# Local API server ishlatilsa 2GB, aks holda standart 50MB
MAX_TELEGRAM_AUDIO_SIZE_BYTES = int(os.environ.get("MAX_TELEGRAM_AUDIO_SIZE_BYTES", 2 * 1024 * 1024 * 1024))

# Local Telegram Bot API server manzili
TELEGRAM_LOCAL_API_URL = os.environ.get("TELEGRAM_LOCAL_API_URL", "")

# ============================================================
# NEW FEATURE CONFIGURATIONS
# ============================================================
# Vinyl markazidagi matn (Artist / Song Title) yoqish/o'chirish
ENABLE_LABEL_TEXT = os.environ.get("ENABLE_LABEL_TEXT", "true").lower() in ("true", "1", "yes")

# Rate Limiting (So'rovlar soni va vaqt oynasi soniyalarda)
RATE_LIMIT_JOBS = int(os.environ.get("RATE_LIMIT_JOBS", 5))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", 60))

# Watermark / Bot imzosi
BOT_SIGNATURE = os.environ.get("BOT_SIGNATURE", "@PlastinkamBot")

# Playlist / Album batch debounce timeout (soniyalarda)
BATCH_DEBOUNCE_SECONDS = float(os.environ.get("BATCH_DEBOUNCE_SECONDS", 3.0))

# ============================================================
# CUSTOM EMOJI CONFIGURATION (Telegram Custom Emoji ID lari)
# ============================================================
# Bot xabarlaridagi Custom Emoji ID lar:
EMOJI_HOURGLASS = os.environ.get("EMOJI_HOURGLASS", "")        # ⏳ Status animatsiyasidagi qumsoat
EMOJI_WARNING = os.environ.get("EMOJI_WARNING", "")            # ⚠️ Ogohlantirishlar
EMOJI_ERROR = os.environ.get("EMOJI_ERROR", "")                # ❌ Xatolar
EMOJI_SUCCESS = os.environ.get("EMOJI_SUCCESS", "")            # ✅ Muvaffaqiyatlar / Saqlandi
EMOJI_MUSIC = os.environ.get("EMOJI_MUSIC", "")                # 🎵 Audio xabarlar
EMOJI_CAMERA = os.environ.get("EMOJI_CAMERA", "")              # 📷 Rasm kutish xabari
EMOJI_CANCEL = os.environ.get("EMOJI_CANCEL", "")              # 🗑️ Bekor qilish xabari
EMOJI_PALETTE = os.environ.get("EMOJI_PALETTE", "")            # 🎨 Shablon tanlash xabari
EMOJI_SPEED = os.environ.get("EMOJI_SPEED", "")                # 🎛️ Tezlik tanlash xabari
EMOJI_CHECK = os.environ.get("EMOJI_CHECK", "")                # ✅ Tanlangan tugma indikatori
EMOJI_TIME = os.environ.get("EMOJI_TIME", "")                  # ⏰ Taymer / Kutish muddati
EMOJI_TRIM = os.environ.get("EMOJI_TRIM", "")                  # ✂️ Audio kesish xabari
EMOJI_WATERMARK = os.environ.get("EMOJI_WATERMARK", "")        # 🏷️ Watermark xabari
EMOJI_STATS = os.environ.get("EMOJI_STATS", "")                # 📊 Statistika xabari
EMOJI_BATCH = os.environ.get("EMOJI_BATCH", "")                # 📦 Albom / Playlist xabari
EMOJI_LABEL = os.environ.get("EMOJI_LABEL", "")                # ✍️ Matn tahrirlash xabari

# Inline Keyboard tugmalaridagi Custom Emoji Icon ID lar (icon_custom_emoji_id):
BTN_EMOJI_ADD_IMAGE = os.environ.get("BTN_EMOJI_ADD_IMAGE", "")         # "Rasm qo'shish" tugmasi
BTN_EMOJI_CANCEL = os.environ.get("BTN_EMOJI_CANCEL", "")             # "Bekor qilish" tugmasi
BTN_EMOJI_VINYL_PINK = os.environ.get("BTN_EMOJI_VINYL_PINK", "")       # "Pushti rang" tugmasi
BTN_EMOJI_VINYL_DEFAULT = os.environ.get("BTN_EMOJI_VINYL_DEFAULT", "")   # "Oddiysidan foydalanish" tugmasi
BTN_EMOJI_VINYL_YELLOW = os.environ.get("BTN_EMOJI_VINYL_YELLOW", "")   # "Sariq rang" tugmasi
BTN_EMOJI_VINYL_BLUE = os.environ.get("BTN_EMOJI_VINYL_BLUE", "")       # "Ko'k rang" tugmasi
BTN_EMOJI_SPEED = os.environ.get("BTN_EMOJI_SPEED", "")               # Speed tugmalari uchun emoji
BTN_EMOJI_SPEED_ACTIVE = os.environ.get("BTN_EMOJI_SPEED_ACTIVE", "")   # Faol tezlik tugmasi emojisi
BTN_EMOJI_SPEED_INACTIVE = os.environ.get("BTN_EMOJI_SPEED_INACTIVE", "") # Tanlanmagan tezlik emojisi
BTN_EMOJI_CONTINUE = os.environ.get("BTN_EMOJI_CONTINUE", "")         # "Davom etish" tugmasi emojisi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")
DEFAULT_FONT_PATH = os.path.join(FONTS_DIR, "font.ttf")

VINYL_PATH = os.path.join(ASSETS_DIR, "vinyl.png")
VINYL_PINK_PATH = os.path.join(ASSETS_DIR, "vinyl_pink.png")
VINYL_BLUE_PATH = os.path.join(ASSETS_DIR, "vinyl_blue.png")
SHADOW_PATH = os.path.join(ASSETS_DIR, "shadow.png")
SHADOW_PINK_PATH = os.path.join(ASSETS_DIR, "shadow_pink.png")
SHADOW_BLUE_PATH = os.path.join(ASSETS_DIR, "shadow_blue.png")
VINYL_YELLOW_PATH = os.path.join(ASSETS_DIR, "vinyl_yellow.png")
SHADOW_YELLOW_PATH = os.path.join(ASSETS_DIR, "shadow_yellow.png")
TEMP_DIR = os.path.join(BASE_DIR, "temp")
