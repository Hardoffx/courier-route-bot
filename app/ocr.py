from __future__ import annotations

from dataclasses import dataclass
import re

import cv2
import numpy as np
import pytesseract

from app.catalog import resolve_known_full
from app.normalizer import normalize_address


@dataclass
class OCRRow:
    raw_text: str
    nav_address: str
    lab_type: str
    facility_code: str | None = None
    window_start: str | None = None
    window_end: str | None = None


def _classify_color(crop: np.ndarray) -> str:
    if crop.size == 0:
        return "UNKNOWN"
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    inner = hsv[max(1, int(h * .12)):max(2, int(h * .88)), max(1, int(w * .02)):max(2, int(w * .98))]
    sat = inner[:, :, 1]
    val = inner[:, :, 2]
    mask = (sat > 18) & (val > 90)
    if not np.any(mask):
        return "UNKNOWN"
    hue = float(np.median(inner[:, :, 0][mask]))
    sat_med = float(np.median(sat[mask]))
    if 33 <= hue <= 95 and sat_med > 25:
        return "INVITRO"
    if 17 <= hue < 33 and sat_med > 25:
        return "CMD"
    if 3 <= hue < 17 and sat_med > 20:
        return "OTHER"
    return "UNKNOWN"


def _group_positions(values: list[int]) -> list[int]:
    groups: list[list[int]] = []
    for value in values:
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [round(sum(g) / len(g)) for g in groups]


def _row_bounds(img: np.ndarray) -> list[tuple[int, int]]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    inv = 255 - gray
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, img.shape[1] // 3), 1))
    lines_img = cv2.morphologyEx(inv, cv2.MORPH_OPEN, kernel)
    projection = lines_img.mean(axis=1)
    threshold = max(12, projection.max() * .30)
    ys = np.where(projection >= threshold)[0].tolist()
    lines = _group_positions(ys)
    if not lines:
        return [(0, img.shape[0])]

    bounds: list[tuple[int, int]] = []
    first_h = lines[0]
    if 10 <= first_h <= 180:
        bounds.append((0, lines[0]))

    bounds.extend((a + 1, b) for a, b in zip(lines, lines[1:]) if 10 <= b - a <= 180)

    last_h = img.shape[0] - 1 - lines[-1]
    if 10 <= last_h <= 180:
        bounds.append((lines[-1] + 1, img.shape[0]))

    return bounds or [(0, img.shape[0])]


def _column_bounds(img: np.ndarray) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    """Return (left/code, address, time) column bounds for full or cropped route sheets."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    inv = 255 - gray
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, img.shape[0] // 4)))
    lines_img = cv2.morphologyEx(inv, cv2.MORPH_OPEN, kernel)
    projection = lines_img.mean(axis=0)
    threshold = max(12, projection.max() * .30)
    xs = _group_positions(np.where(projection >= threshold)[0].tolist())
    xs = [x for x in xs if 0 < x < img.shape[1]]

    if len(xs) >= 4:
        left_end, addr_end, blank_end, time_end = xs[-4:]
        left_start = xs[-5] if len(xs) >= 5 else 0
        return (
            (left_start + 2, left_end - 2),
            (left_end + 2, addr_end - 2),
            (blank_end + 2, min(time_end - 2, img.shape[1] - 1)),
        )
    return None


def _ocr(crop: np.ndarray, psm: int = 6, lang: str = "rus") -> str:
    if crop.size == 0:
        return ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(bw, lang=lang, config=f"--oem 3 --psm {psm}")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _ocr_digits(crop: np.ndarray) -> str:
    if crop.size == 0:
        return ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return pytesseract.image_to_string(
        bw,
        lang="eng",
        config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789/",
    ).strip()


def _ocr_time(crop: np.ndarray) -> tuple[str | None, str | None]:
    if crop.size == 0:
        return None, None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(
        bw,
        lang="eng",
        config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789:-",
    ).strip()
    m = re.search(r"([0-2]?\d[:.]\d{2})\s*[-–—]\s*([0-2]?\d[:.]\d{2})", text)
    if not m:
        return None, None
    return m.group(1).replace(".", ":"), m.group(2).replace(".", ":")


def _facility_code(text: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{2,6})(?:\s*/|\b)", text.replace(" ", ""))
    return m.group(1) if m else None


def _left_is_cmd_code(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    compact = compact.replace("|", "").replace("\\", "/")
    compact = re.sub(r"^[^0-9]+|[^0-9/]+$", "", compact)
    return bool(re.fullmatch(r"\d{2,6}(?:/\d{2,6})*", compact))


def _left_is_known_other_lab(text: str) -> bool:
    t = re.sub(r"[^а-яa-z0-9]+", "", (text or "").lower().replace("ё", "е"))
    return t in {"литех", "liteh", "litek", "литex", "литэх"}


def _infer_lab_from_left(raw_left: str, color_lab: str) -> str:
    if _left_is_cmd_code(raw_left):
        return "CMD"
    if _left_is_known_other_lab(raw_left):
        return "OTHER"
    if color_lab != "UNKNOWN":
        return color_lab
    if re.search(r"[А-Яа-яA-Za-z]", raw_left or ""):
        return "INVITRO"
    return "UNKNOWN"


def _looks_like_address(text: str) -> bool:
    t = text.lower()
    hints = ("москва", "красногорск", "обл", "ул", "пер", "б-р", "ш ", "тер", "р-н", "аристово", "юрлово")
    return len(text) >= 6 and any(x in t for x in hints)


def _extract_address_from_full_row(text: str) -> str:
    """Recover an address when column detection/OCR is weak on an alternate sheet theme."""
    if not text:
        return ""
    # Start at the first strong geographical marker. This intentionally keeps
    # postal codes out and ignores whatever laboratory label precedes it.
    m = re.search(
        r"\b(?:Москва|Московская\s+обл|г\s+Красногорск|Красногорск|Новое\s+Аристово|Юрлово)\b",
        text,
        flags=re.I,
    )
    if not m:
        return ""
    candidate = text[m.start():].strip()
    # Full-row OCR can append the time column; strip it from the navigation text.
    candidate = re.sub(r"\s+[0-2]?\d[:.]\d{2}\s*[-–—]\s*[0-2]?\d[:.]\d{2}\s*$", "", candidate)
    candidate = re.sub(r"\s+БМ\s*$", "", candidate, flags=re.I)
    return candidate.strip(" |,;:-")


def _is_service_point(address: str) -> bool:
    t = address.lower().replace("ё", "е")
    return "прядильн" in t or t.startswith("склад") or "офис прядиль" in t


def extract_rows(image_path: str) -> list[OCRRow]:
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Не удалось открыть изображение")

    columns = _column_bounds(img)
    rows: list[OCRRow] = []
    for y1, y2 in _row_bounds(img):
        crop = img[y1:y2, :]
        color_lab = _classify_color(crop)
        lab = color_lab

        if columns:
            (lx1, lx2), (ax1, ax2), (tx1, tx2) = columns
            pad_y1 = min(y2 - y1 - 1, 1)
            pad_y2 = max(pad_y1 + 1, y2 - y1 - 1)
            left_crop = crop[pad_y1:pad_y2, lx1:lx2]
            address_crop = crop[pad_y1:pad_y2, ax1:ax2]
            time_crop = crop[pad_y1:pad_y2, tx1:tx2]
            raw_address = _ocr(address_crop, psm=6)
            raw_left = _ocr(left_crop, psm=7)
            raw_digits = _ocr_digits(left_crop)
            start, end = _ocr_time(time_crop)

            lab = _infer_lab_from_left(raw_left, color_lab)
            code = _facility_code(raw_digits) if lab == "CMD" else None

            # Some logistic exports use different fills/borders and Tesseract
            # occasionally loses the address column for a whole row (notably
            # third-party labs such as ЛИТЕХ). OCR the entire row as a fallback
            # and recover the address from geographical markers.
            full_row = ""
            if not _looks_like_address(raw_address) or lab == "OTHER":
                full_row = _ocr(crop, psm=6)
                recovered = _extract_address_from_full_row(full_row)
                if recovered and (_looks_like_address(recovered) or not _looks_like_address(raw_address)):
                    raw_address = recovered
            raw = " ".join(x for x in (raw_left, raw_address) if x)
            if full_row and _left_is_known_other_lab(raw_left):
                # Keep the explicit lab name in raw_text so the UI can display it.
                raw = " ".join(x for x in (raw_left, raw_address) if x)
        else:
            raw = _ocr(crop)
            raw_address = _extract_address_from_full_row(raw) or raw
            raw_left = ""
            start = end = None
            code = None

        if not _looks_like_address(raw_address):
            continue
        nav = normalize_address(raw_address)
        if _is_service_point(nav):
            continue

        known = resolve_known_full(nav)
        if known:
            nav = known.nav_address
            lab = known.lab_type
            if lab == "CMD" and not code:
                code = known.facility_code

        rows.append(
            OCRRow(
                raw_text=raw,
                nav_address=nav,
                lab_type=lab,
                facility_code=code,
                window_start=start,
                window_end=end,
            )
        )
    return rows
