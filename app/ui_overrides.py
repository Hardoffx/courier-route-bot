from __future__ import annotations


def apply(bot_module) -> None:
    """Apply small presentation-only overrides without touching route logic."""

    def point_text(point, index, total):
        state = "✅ <b>ВЫПОЛНЕНО</b>\n" if point["done"] else "➡️ <b>ТЕКУЩАЯ ТОЧКА</b>\n"
        source = " · ➕ доп." if point["source"] == "MANUAL" else ""

        blocks = [f"{state}<b>{index + 1} из {total}</b> · {bot_module.badge(point['lab_type'])}{source}"]

        window = bot_module.window_text(point)
        if window:
            blocks.append(f"🕓 <b>{window}</b>")

        if point.get("lab_type") == "CMD" and point.get("facility_code"):
            blocks.append(f"🏥 ЛПУ №<b>{point['facility_code']}</b>")

        phone = bot_module.format_phone(point.get("phone"))
        if phone:
            blocks.append(f"📞 {phone}")

        if point.get("note"):
            blocks.append(f"📝 <b>Не забыть:</b> {point['note']}")

        # Keep every operational field visually separated for quick reading while driving.
        # Address stays the final information block immediately above the action buttons.
        blocks.append(f"<b>{point['nav_address']}</b>")
        return "\n\n".join(blocks)

    bot_module.point_text = point_text
