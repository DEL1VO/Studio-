"""
Разбор листа спрайтов персонажа (character sheet / asset pack из ChatGPT) на части.

Лист может быть любой раскладки: головы с эмоциями, рты, глаза, руки, позы в полный рост.
Фон — «шахматка» прозрачности, нарисованная прямо в картинке (не настоящая прозрачность).

Что делает:
  1. находит фон-шахматку (светлый, почти без цвета) и вырезает его;
  2. режет лист на отдельные спрайты (связные области);
  3. раскладывает по папкам: body / head / mouth / eye / hand / other;
  4. пишет meta.json и contact.png (картинка с номерами — для проверки).

Запуск:  python split_sheet.py sheet.png out_dir
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage as ndi

MIN_AREA = 250          # мельче — мусор от шахматки


def foreground_mask(rgb: np.ndarray) -> np.ndarray:
    """True там, где персонаж. Шахматка = светлая и почти бесцветная."""
    r, g, b = [rgb[..., i].astype(int) for i in range(3)]
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)
    checker = (mx > 185) & ((mx - mn) < 22)
    fg = ~checker
    fg = ndi.binary_opening(fg, iterations=1)           # убрать одиночные пиксели
    fg = ndi.binary_closing(fg, iterations=3)           # склеить тонкие разрывы контура
    return fg


def classify(box, img_wh, area, mean_rgb, fill):
    """Грубая классификация спрайта по размеру/форме/цвету."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    W, H = img_wh
    ratio = w / h
    skin = (mean_rgb[0] > mean_rgb[2] + 25) and mean_rgb[0] > 120     # тёплый, светлый
    # маленькие широкие -> рты и глаза
    if h < 0.07 * H and ratio > 1.3:
        return "mouth" if (mean_rgb[0] > mean_rgb[1] + 35) else "eye"
    if h < 0.10 * H and w < 0.12 * W and skin:
        return "hand"
    if h > 0.30 * H and ratio < 0.6:
        return "body"
    if 0.7 < ratio < 1.35 and 0.08 * H < h < 0.30 * H:
        return "head"
    if skin and h < 0.16 * H:
        return "hand"
    return "other"


def split(src: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    im = Image.open(src).convert("RGB")
    rgb = np.array(im)
    H, W = rgb.shape[:2]
    fg = foreground_mask(rgb)
    lab, n = ndi.label(fg)
    objs = ndi.find_objects(lab)
    items = []
    for i, sl in enumerate(objs, start=1):
        comp = lab[sl] == i
        area = int(comp.sum())
        if area < MIN_AREA:
            continue
        comp = ndi.binary_fill_holes(comp)               # зубы/белки внутри остаются
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        crop = np.array(im.crop((x0, y0, x1, y1)))
        mean = crop[comp].mean(axis=0)
        kind = classify((x0, y0, x1, y1), (W, H), area, mean, area / max(1, (x1 - x0) * (y1 - y0)))
        alpha = Image.fromarray((comp * 255).astype("uint8")).filter(ImageFilter.GaussianBlur(0.7))
        spr = Image.fromarray(crop).convert("RGBA")
        spr.putalpha(alpha)
        items.append({"kind": kind, "box": [x0, y0, x1, y1], "img": spr})

    # порядок: сверху вниз, слева направо (по строкам)
    items.sort(key=lambda d: (round(d["box"][1] / (0.06 * H)), d["box"][0]))
    meta = {"source": str(src), "size": [W, H], "sprites": []}
    counters = {}
    for idx, it in enumerate(items):
        k = it["kind"]
        counters[k] = counters.get(k, 0) + 1
        d = out_dir / k
        d.mkdir(exist_ok=True)
        p = d / f"{k}_{counters[k]:02d}.png"
        it["img"].save(p)
        meta["sprites"].append({"id": idx, "kind": k, "file": str(p), "box": it["box"]})
    meta["counts"] = counters
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))

    # контактный лист с номерами для проверки глазами
    cs = im.copy()
    dr = ImageDraw.Draw(cs)
    col = {"body": "red", "head": "blue", "mouth": "magenta", "eye": "green", "hand": "orange", "other": "gray"}
    for s in meta["sprites"]:
        x0, y0, x1, y1 = s["box"]
        dr.rectangle((x0, y0, x1, y1), outline=col[s["kind"]], width=2)
        dr.text((x0 + 2, y0 + 2), f'{s["id"]}', fill=col[s["kind"]])
    cs.save(out_dir / "contact.png")
    return meta


if __name__ == "__main__":
    m = split(Path(sys.argv[1]), Path(sys.argv[2]))
    print(m["counts"])
