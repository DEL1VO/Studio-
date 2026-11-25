"""
Подготовка персонажа из листа ChatGPT (character sheet) для мульт-студии.

Вход:  PNG-лист персонажа (вид спереди + сетка 3x3 эмоций лица справа сверху).
Выход: папка с
  body.png        — персонаж в полный рост, фон вырезан (прозрачный)
  face_0..8.png   — 9 выражений лица (0 нейтральное, 3 улыбка, 4 крик/рот открыт, 5 удивление ...)
  meta.json       — координаты головы/лица в body.png (0..1, от левого нижнего угла)

Если картинка — не такой лист (одиночный персонаж), копируется как есть, без эмоций.
Запуск:  python prepare_character.py sheet.png out_dir
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from split_sheet import split

# Раскладка листа в долях от размера картинки (подобрана под формат ChatGPT 3:2)
FRONT_BOX = (0.010, 0.010, 0.199, 0.850)     # персонаж спереди
GRID_BOX = (0.779, 0.017, 0.996, 0.421)      # 9 эмоций
# лицо внутри вырезанного body.png (доли ширины/высоты, сверху вниз)
HEAD_REL = (0.38, 0.00, 0.72, 0.15)


def cut_background(img: Image.Image, thresh: int = 34) -> Image.Image:
    """Заливка однотонного серого фона от краёв -> прозрачность."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    marker = (255, 0, 255)
    work = rgb.copy()
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (0, h // 2),
             (w - 1, h // 2), (w // 4, h - 1), (3 * w // 4, h - 1), (w // 2, h // 2 + h // 4)]
    for s in seeds:
        if work.getpixel(s) != marker:
            ImageDraw.floodfill(work, s, marker, thresh=thresh)
    mask = Image.new("L", (w, h), 255)
    px, mp = work.load(), mask.load()
    for y in range(h):
        for x in range(w):
            if px[x, y] == marker:
                mp[x, y] = 0
    mask = mask.filter(ImageFilter.GaussianBlur(0.8))
    out = rgb.convert("RGBA")
    out.putalpha(mask)
    return out


def has_checker(im: Image.Image) -> bool:
    """Фон-«шахматка» прозрачности, нарисованная в картинке (спрайт-лист)."""
    a = np.array(im.convert("RGB").resize((256, 256))).astype(int)
    mx, mn = a.max(axis=2), a.min(axis=2)
    return float(((mx > 185) & (mx - mn < 22)).mean()) > 0.25


def prepare_sprite_sheet(src: Path, out_dir: Path) -> dict:
    """Лист-«ассет-пак» на шахматке: режем на части, берём тело и головы."""
    parts = split(src, out_dir / "parts")
    sp = parts["sprites"]

    def hw(s):
        x0, y0, x1, y1 = s["box"]
        return x1 - x0, y1 - y0

    bodies = [s for s in sp if s["kind"] == "body"]
    tall = [s for s in bodies if hw(s)[1] / hw(s)[0] > 2.0] or bodies
    if not tall:
        raise RuntimeError("На листе не нашёл персонажа в полный рост.")
    body = max(tall, key=lambda s: hw(s)[1])
    heads = [s for s in sp if s["kind"] == "head" and 0.7 < hw(s)[0] / hw(s)[1] < 1.35][:9]
    faces = [h["file"] for h in heads]
    bw, bh = Image.open(body["file"]).size
    meta = {"sheet": "sprites", "body": body["file"], "faces": faces, "size": [bw, bh],
            "face_rect": [0.30, 0.88, 0.70, 1.0]}
    meta["mouth"] = [0.5, 0.93]
    meta["eye_l"], meta["eye_r"] = [0.42, 0.95], [0.58, 0.95]
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return meta


def prepare(src: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    im = Image.open(src).convert("RGB")
    if has_checker(im):
        return prepare_sprite_sheet(src, out_dir)
    W, H = im.size
    is_sheet = abs(W / H - 1.5) < 0.1 and W >= 1200   # лист ChatGPT 3:2
    meta = {"sheet": is_sheet, "faces": []}

    if is_sheet:
        front = im.crop([int(FRONT_BOX[0] * W), int(FRONT_BOX[1] * H),
                         int(FRONT_BOX[2] * W), int(FRONT_BOX[3] * H)])
        grid = im.crop([int(GRID_BOX[0] * W), int(GRID_BOX[1] * H),
                        int(GRID_BOX[2] * W), int(GRID_BOX[3] * H)])
        gw, gh = grid.size
        for i in range(9):
            r, c = divmod(i, 3)
            cell = grid.crop((c * gw // 3 + 2, r * gh // 3 + 2,
                              (c + 1) * gw // 3 - 2, (r + 1) * gh // 3 - 2))
            p = out_dir / f"face_{i}.png"
            cut_background(cell, thresh=30).save(p)
            meta["faces"].append(str(p))
    else:
        front = im

    body = cut_background(front)
    body.save(out_dir / "body.png")
    meta["body"] = str(out_dir / "body.png")
    meta["size"] = body.size
    # рот/глаза относительно body.png (от левого нижнего угла)
    x0, y0, x1, y1 = HEAD_REL
    meta["face_rect"] = [x0, 1 - y1, x1, 1 - y0]      # x0, ybottom, x1, ytop
    meta["mouth"] = [(x0 + x1) / 2, 1 - (y0 + (y1 - y0) * 0.78)]
    meta["eye_l"] = [x0 + (x1 - x0) * 0.32, 1 - (y0 + (y1 - y0) * 0.50)]
    meta["eye_r"] = [x0 + (x1 - x0) * 0.68, 1 - (y0 + (y1 - y0) * 0.50)]
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return meta


if __name__ == "__main__":
    print(json.dumps(prepare(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, indent=1))
