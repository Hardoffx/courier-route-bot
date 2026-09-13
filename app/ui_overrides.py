from __future__ import annotations

import re


def apply(bot_module) -> None:
    """Apply small presentation-only overrides without touching route logic."""

    def other_lab_name(point) -> str | None:
        if point.get("lab_type") != "OTHER":
            return None
        raw = (point.get("raw_text") or "").strip()
        if not raw:
            return None

        # OCR stores the left column first and the address second. Split at the
        # first recognizable address marker and keep only the laboratory name.
        m = re.search(
            r"\b(?:\d{6}\s*,\s*)?(?:Москва|Московская\s+обл|г\s+Красногорск|Красногорск|Новое\s+Аристово|Юрлово)\b",
            raw,
            flags=re.I,
        )
        name = raw[:m.start()].strip(" |,;:-") if m else raw.strip(" |,;:-")
        name = re.sub(r"\s+", " ", name)

        # Avoid displaying OCR garbage as a lab name.
        if not name or len(name) > 40 or not re.search(r"[А-Яа-яA-Za-z]", name):
            return None
        return name

    def point_text(point, index, total):
        state = "✅ <b>ВЫПОЛНЕНО</b>\n" if point["done"] else "➡️ <b>ТЕКУЩАЯ ТОЧКА</b>\n"
        source = " · ➕ доп." if point["source"] == "MANUAL" else ""

        lab_name = other_lab_name(point)
        if lab_name:
            lab_badge = f"🟠 {lab_name}"
        else:
            lab_badge = bot_module.badge(point["lab_type"])

        blocks = [f"{state}<b>{index + 1} из {total}</b> · {lab_badge}{source}"]

        window = bot_module.window_text(point)

        # For CMD, the LPU number is the primary visual identifier, so show it
        # before the collection window. Other labs keep the time first.
        if point.get("lab_type") == "CMD":
            if point.get("facility_code"):
                blocks.append(f"🏥 ЛПУ №<b>{point['facility_code']}</b>")
            if window:
                blocks.append(f"🕓 <b>{window}</b>")
        elif window:
            blocks.append(f"🕓 <b>{window}</b>")

        raw_phone = point.get("phone")
        phone = bot_module.format_phone(raw_phone)
        if phone:
            # Use an explicit tel: link instead of relying on each Telegram client
            # to auto-detect the formatted number. This keeps Android and iPhone
            # behavior consistent while still showing the human-readable number.
            blocks.append(f'📞 <a href="tel:{raw_phone}">{phone}</a>')

        if point.get("note"):
            blocks.append(f"📝 <b>Не забыть:</b> {point['note']}")

        blocks.append(f"<b>{point['nav_address']}</b>")
        return "\n\n".join(blocks)

    bot_module.point_text = point_text
