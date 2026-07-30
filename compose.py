"""
Muqova rasmini (thumbnail) aylanuvchi diskning teshigi ichiga joylashtiradi.
Natija: bitta PNG rasm (disk + ichiga yopishtirilgan rasm), keyin processor.py
faylida aylantirish uchun tayyor bo'ladi.
"""
from PIL import Image, ImageDraw


def load_vinyl_template(vinyl_path: str, size: int = 640) -> Image.Image:
    template = Image.open(vinyl_path).convert("RGBA")
    if template.size != (size, size):
        template = template.resize((size, size))
    return template


def build_disc(thumb_path: str, vinyl_path: str, out_path: str,
                hole_ratio: float = 0.42, size: int = 640) -> str:
    vinyl = load_vinyl_template(vinyl_path, size)

    hole_d = int(size * hole_ratio)

    label = Image.open(thumb_path).convert("RGBA")
    w, h = label.size
    m = min(w, h)
    label = label.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m))

    max_label_size = max(1, int(hole_d * 0.96))
    label = label.resize((max_label_size, max_label_size))

    mask = Image.new("L", label.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, max_label_size - 1, max_label_size - 1), fill=255)
    label.putalpha(mask)

    pos = ((size - max_label_size) // 2, (size - max_label_size) // 2)
    vinyl.alpha_composite(label, pos)

    vinyl.save(out_path)
    return out_path
