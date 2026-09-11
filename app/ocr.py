from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract

from app.normalizer import normalize_address


@dataclass
class OCRRow:
    raw_text: str
    nav_address: str
    lab_type: str


def _classify_color(crop: np.ndarray) -> str:
    if crop.size == 0:
        return "UNKNOWN"
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    inner = hsv[max(1, int(h * .15)):max(2, int(h * .85)), max(1, int(w * .03)):max(2, int(w * .97))]
    sat = inner[:, :, 1]
    val = inner[:, :, 2]
    mask = (sat > 25) & (val > 100)
    if not np.any(mask):
        return "UNKNOWN"
    hue = float(np.median(inner[:, :, 0][mask]))
    sat_med = float(np.median(sat[mask]))
    if 35 <= hue <= 90 and sat_med > 35:
        return "INVITRO"
    if 18 <= hue < 35 and sat_med > 35:
        return "CMD"
    if 4 <= hue < 18 and sat_med > 25:
        return "OTHER"
    return "UNKNOWN"


def _row_bounds(img: np.ndarray) -> list[tuple[int, int]]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    inv = 255 - gray
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, img.shape[1] // 3), 1))
    lines_img = cv2.morphologyEx(inv, cv2.MORPH_OPEN, kernel)
    projection = lines_img.mean(axis=1)
    threshold = max(12, projection.max() * .30)
    ys = np.where(projection >= threshold)[0].tolist()

    groups: list[list[int]] = []
    for y in ys:
        if not groups or y > groups[-1][-1] + 1:
            groups.append([y])
        else:
            groups[-1].append(y)
    lines = [round(sum(g) / len(g)) for g in groups]
    bounds = [(a + 1, b) for a, b in zip(lines, lines[1:]) if 15 <= b - a <= 180]
    return bounds or [(0, img.shape[0])]


def _ocr(crop: np.ndarray) -> str:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(bw, lang="rus", config="--oem 3 --psm 6")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _looks_like_address(text: str) -> bool:
    t = text.lower()
    hints = ("москва", "красногорск", "обл", "ул", "пер", "б-р", "ш ", "тер", "р-н", "аристово", "юрлово")
    return len(text) >= 6 and any(x in t for x in hints)


def extract_rows(image_path: str) -> list[OCRRow]:
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Не удалось открыть изображение")

    rows: list[OCRRow] = []
    for y1, y2 in _row_bounds(img):
        crop = img[y1:y2, :]
        raw = _ocr(crop)
        if not _looks_like_address(raw):
            continue
        rows.append(OCRRow(raw_text=raw, nav_address=normalize_address(raw), lab_type=_classify_color(crop)))
    return rows
