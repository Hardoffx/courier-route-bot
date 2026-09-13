from __future__ import annotations


def apply(bot_module) -> None:
    """Apply small presentation-only overrides without touching route logic."""

    def point_text(point, index, total):
        state = "✅ <b>ВЫПОЛНЕНО</b>\n" if point["done"] else "➡️ <b>ТЕКУЩАЯ ТОЧКА</b>\n"
        source = " · ➕ доп." if point["source"] == "MANUAL" else ""

        lines = [
            f"{state}<b>{index + 1} из {total}</b> · {bot_module.badge(point['lab_type'])}{source}"
        ]

        window = bot_module.window_text(point)
        if window:
            lines.append(f"🕓 Забор: <b>{window}</b>")

        if point.get("lab_type") == "CMD" and point.get("facility_code"):
            lines.append(f"🏥 ЛПУ №<b>{point['facility_code']}</b>")

        phone = bot_module.format_phone(point.get("phone"))
        if phone:
            lines.append(f"📞 {phone}")

        if point.get("note"):
            lines.append(f"📝 <b>Не забыть:</b> {point['note']}")

        # Address is intentionally always the final line: this is the most useful
        # visual anchor immediately above the action buttons while driving.
        lines.append(f"\n<b>{point['nav_address']}</b>")
        return "\n".join(lines)

    bot_module.point_text = point_text
