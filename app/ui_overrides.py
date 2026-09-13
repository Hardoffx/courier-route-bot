from __future__ import annotations

from collections import Counter
import re


def apply(bot_module) -> None:
    """Apply small presentation-only overrides without touching route logic."""

    def other_lab_name(point) -> str | None:
        if point.get("lab_type") != "OTHER":
            return None
        raw = (point.get("raw_text") or "").strip()
        if not raw:
            return None

        m = re.search(
            r"\b(?:\d{6}\s*,\s*)?(?:Москва|Московская\s+обл|г\s+Красногорск|Красногорск|Новое\s+Аристово|Юрлово)\b",
            raw,
            flags=re.I,
        )
        name = raw[:m.start()].strip(" |,;:-") if m else raw.strip(" |,;:-")
        name = re.sub(r"\s+", " ", name)

        if not name or len(name) > 40 or not re.search(r"[А-Яа-яA-Za-z]", name):
            return None
        return name

    def summary_text(points, diff_text=None):
        invitro = sum(1 for p in points if p.get("lab_type") == "INVITRO")
        cmd = sum(1 for p in points if p.get("lab_type") == "CMD")
        unknown = sum(1 for p in points if p.get("lab_type") == "UNKNOWN")
        done = sum(int(p.get("done", 0)) for p in points)
        left = len(points) - done

        other_names = Counter()
        generic_other = 0
        for p in points:
            if p.get("lab_type") != "OTHER":
                continue
            name = other_lab_name(p)
            if name:
                other_names[name] += 1
            else:
                generic_other += 1

        lab_lines = [
            f"🟢 INVITRO: {invitro} · 🟡 CMD: {cmd}",
        ]
        other_parts = [f"🟠 {name}: {count}" for name, count in other_names.items()]
        if generic_other:
            other_parts.append(f"🟠 Другие: {generic_other}")
        if other_parts:
            lab_lines.append(" · ".join(other_parts))
        lab_lines.append(f"⚪ ?: {unknown}")

        progress = f"\n\n✅ Выполнено: <b>{done}/{len(points)}</b> · осталось <b>{left}</b>" if done else ""
        diff = f"\n\n{diff_text}" if diff_text else ""
        return f"🚚 <b>Маршрут</b>\n\nТочек: <b>{len(points)}</b>\n" + "\n".join(lab_lines) + progress + diff

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
            blocks.append(f'📞 <a href="tel:{raw_phone}">{phone}</a>')

        if point.get("note"):
            blocks.append(f"📝 <b>Не забыть:</b> {point['note']}")

        blocks.append(f"<b>{point['nav_address']}</b>")
        return "\n\n".join(blocks)

    bot_module.summary_text = summary_text
    bot_module.point_text = point_text
