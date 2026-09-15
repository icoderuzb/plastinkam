"""
Muqova rasmini (thumbnail) aylanuvchi dis

kning teshigi ichiga joylashtiradi,
shuningdek ixtiyoriy ravishda disk ustiga ijrochi/qo'shiq nomi matnini (Label text)
va bot belgisini (Watermark) chizadi.
"""
import math
import os
from typing import Optional
from PIL import Image, ImageDraw, ImageFont

import config


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Tizimdagi yoki assets/fonts ichidagi shriftni yuklaydi (Cyrillic & Latin qo'llab-quvvatlaydi)."""
    candidate_paths = [
        config.DEFAULT_FONT_PATH,
        os.path.join(config.FONTS_DIR, "font.ttf"),
        os.path.join(config.FONTS_DIR, "DejaVuSans-Bold.ttf"),
        os.path.join(config.FONTS_DIR, "Roboto-Bold.ttf"),
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidate_paths:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def load_vinyl_template(vinyl_path: str, size: int = 640) -> Image.Image:
    template = Image.open(vinyl_path).convert("RGBA")
    if template.size != (size, size):
        template = template.resize((size, size))
    return template


def fit_text_with_ellipsis(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_font_size: int = 24,
    min_font_size: int = 14,
) -> tuple[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    """Binary search orqali shrift o'lchamini max_width ga moslaydi.
    Agar min_font_size da ham sig'masa, matn oxiriga '...' (ellipsis) qo'yib qisqartiradi.
    """
    if not text:
        return "", get_font(min_font_size)

    low = min_font_size
    high = max_font_size
    best_font = get_font(low)
    best_size = low

    while low <= high:
        mid = (low + high) // 2
        f = get_font(mid)
        bbox = draw.textbbox((0, 0), text, font=f)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            best_size = mid
            best_font = f
            low = mid + 1
        else:
            high = mid - 1

    # Minimal fontda ham sig'mayotgan bo'lsa — matnni qisqartirish
    f = get_font(best_size)
    bbox = draw.textbbox((0, 0), text, font=f)
    w = bbox[2] - bbox[0]

    fitted_text = text
    while w > max_width and len(fitted_text) > 4:
        fitted_text = fitted_text[:-2] + "…"
        bbox = draw.textbbox((0, 0), fitted_text, font=f)
        w = bbox[2] - bbox[0]

    return fitted_text, f


def _draw_arc_text(
    image: Image.Image,
    text: str,
    radius: float,
    center: tuple[float, float],
    is_top: bool = True,
    fill=(245, 245, 245, 245),
    stroke_fill=(15, 15, 15, 200),
    stroke_width: int = 1,
    shadow_fill=(10, 10, 10, 180),
    max_font_size: int = 20,
    min_font_size: int = 11,
    max_arc_deg: float = 135.0,
) -> Image.Image:
    """Matnni vinyl plastinka aylanasi bo'ylab yoysimon (curved / circular arc) qilib chizadi."""
    if not text or not text.strip():
        return image

    text = text.strip()
    cx, cy = center
    max_arc_rad = math.radians(max_arc_deg)

    # Shrift o'lchamini yoy burchagiga sig'adigan qilib moslash
    font_size = max_font_size
    while font_size > min_font_size:
        f = get_font(font_size)
        try:
            total_w = sum(f.getlength(c) for c in text)
        except Exception:
            total_w = sum(max(1, f.getbbox(c)[2] - f.getbbox(c)[0]) for c in text)
        if (total_w / radius) <= max_arc_rad:
            break
        font_size -= 1

    font = get_font(font_size)
    try:
        char_widths = [font.getlength(c) for c in text]
    except Exception:
        char_widths = [max(1, font.getbbox(c)[2] - font.getbbox(c)[0]) for c in text]

    total_w = sum(char_widths)
    arc_rad = total_w / radius

    # Agar minimal fontda ham yoydan oshib ketsa, '…' bilan qisqartirish
    if arc_rad > max_arc_rad and len(text) > 4:
        while len(text) > 4 and arc_rad > max_arc_rad:
            text = text[:-2] + "…"
            try:
                char_widths = [font.getlength(c) for c in text]
            except Exception:
                char_widths = [max(1, font.getbbox(c)[2] - font.getbbox(c)[0]) for c in text]
            total_w = sum(char_widths)
            arc_rad = total_w / radius

    if is_top:
        # Yuqori yoy: 12 o'clock (-pi/2), chapdan o'ngga (soat mili bo'yicha)
        start_angle = -math.pi / 2 - (arc_rad / 2)
        direction = 1
    else:
        # Pastki yoy: 6 o'clock (+pi/2), chapdan o'ngga to'g'ri o'qilish
        start_angle = math.pi / 2 + (arc_rad / 2)
        direction = -1

    cur_angle = start_angle
    pad = 6

    for i, c in enumerate(text):
        w = char_widths[i]
        mid_angle = cur_angle + direction * (w / (2.0 * radius))
        cur_angle += direction * (w / radius)

        if c == " " or w <= 0:
            continue

        try:
            bbox = font.getbbox(c)
            gw = max(1, bbox[2] - bbox[0])
            gh = max(1, bbox[3] - bbox[1])
            ox = pad - bbox[0]
            oy = pad - bbox[1]
        except Exception:
            gw, gh = int(font_size), int(font_size)
            ox, oy = pad, pad

        c_img = Image.new("RGBA", (gw + pad * 2, gh + pad * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(c_img)

        # Soya va hoshiya bilan chizish
        d.text((ox + 1, oy + 1), c, font=font, fill=shadow_fill)
        d.text(
            (ox, oy),
            c,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )

        if is_top:
            rot = math.degrees(mid_angle) + 90
        else:
            rot = math.degrees(mid_angle) - 90

        rotated = c_img.rotate(-rot, expand=True, resample=Image.Resampling.BICUBIC)
        px = int(round(cx + radius * math.cos(mid_angle) - rotated.width / 2.0))
        py = int(round(cy + radius * math.sin(mid_angle) - rotated.height / 2.0))

        image.alpha_composite(rotated, (px, py))

    return image


def draw_disc_label(
    disc: Image.Image,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    vinyl_color: str = "default",
    size: int = 640,
) -> Image.Image:
    """Vinyl plastinkasiga ijrochi (tepadagi yoyda) va qo'shiq nomi (pastdagi yoyda) matnini yoysimon chizadi."""
    if not artist and not title:
        return disc

    # Rang va kontrast sozlamalari
    # default (qora) va blue (ko'k) uchun oq yozuv, qora soya
    # yellow (sariq) va pink (pushti) uchun qora yozuv, oq/och soya
    if vinyl_color in ("yellow", "pink"):
        fill_color = (25, 25, 25, 240)
        stroke_color = (255, 255, 255, 180)
        shadow_color = (240, 240, 240, 160)
    else:
        fill_color = (245, 245, 245, 245)
        stroke_color = (15, 15, 15, 200)
        shadow_color = (10, 10, 10, 180)

    cx = size / 2.0
    cy = size / 2.0
    radius_top = size * 0.33      # ~211px
    radius_bottom = size * 0.35   # ~224px

    # 1. Artist (Ijrochi) — tepada nafis yoysimon arch
    if artist:
        disc = _draw_arc_text(
            disc,
            text=artist.upper(),
            radius=radius_top,
            center=(cx, cy),
            is_top=True,
            fill=fill_color,
            stroke_fill=stroke_color,
            shadow_fill=shadow_color,
            max_font_size=20,
            min_font_size=12,
            max_arc_deg=130.0,
        )

    # 2. Title (Qo'shiq nomi) — pastda nafis yoysimon cradle
    if title:
        disc = _draw_arc_text(
            disc,
            text=title,
            radius=radius_bottom,
            center=(cx, cy),
            is_top=False,
            fill=fill_color,
            stroke_fill=stroke_color,
            shadow_fill=shadow_color,
            max_font_size=21,
            min_font_size=12,
            max_arc_deg=135.0,
        )

    return disc


def draw_watermark(
    disc: Image.Image,
    signature: str = "@PlastinkamBot",
    size: int = 640,
) -> Image.Image:
    """Plastinka chetiga nafis bot imzosi (Watermark) belgisini qo'yadi."""
    if not signature:
        return disc

    draw = ImageDraw.Draw(disc)
    font = get_font(13)
    bbox = draw.textbbox((0, 0), signature, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]

    # Pastki qismda o'rtada nafis joylashuv
    x = (size - w) // 2
    y = size - h - 18

    # Yarim shaffof orqa fon (badge)
    padding_x = 8
    padding_y = 3
    rect = [x - padding_x, y - padding_y, x + w + padding_x, y + h + padding_y]
    draw.rounded_rectangle(rect, radius=6, fill=(0, 0, 0, 110))

    # Matn
    draw.text((x, y), signature, font=font, fill=(255, 255, 255, 220))
    return disc


def build_disc(
    thumb_path: str,
    vinyl_path: str,
    out_path: str,
    hole_ratio: float = 0.42,
    size: int = 640,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    vinyl_color: str = "default",
    include_watermark: bool = False,
) -> str:
    """Muqova rasmini diskka joylashtiradi, kerak bo'lsa matn va watermark qo'shadi."""
    vinyl = load_vinyl_template(vinyl_path, size)

    hole_d = int(size * hole_ratio)

    label = Image.open(thumb_path).convert("RGBA")
    w, h = label.size
    m = min(w, h)
    label = label.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m))

    max_label_size = max(1, int(hole_d * 0.96))
    label = label.resize((max_label_size, max_label_size), Image.Resampling.LANCZOS)

    mask = Image.new("L", label.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, max_label_size - 1, max_label_size - 1), fill=255)
    label.putalpha(mask)

    pos = ((size - max_label_size) // 2, (size - max_label_size) // 2)
    vinyl.alpha_composite(label, pos)

    # 1. Matn chizish (agar yoqilgan bo'lsa va artist/title mavjud bo'lsa)
    if config.ENABLE_LABEL_TEXT and (artist or title):
        vinyl = draw_disc_label(
            vinyl, artist=artist, title=title, vinyl_color=vinyl_color, size=size
        )

    # 2. Watermark chizish (agar yoqilgan bo'lsa)
    if include_watermark:
        vinyl = draw_watermark(vinyl, signature=config.BOT_SIGNATURE, size=size)

    vinyl.save(out_path, format="PNG")
    return out_path
