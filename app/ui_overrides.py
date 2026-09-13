from __future__ import annotations


def apply(bot_module) -> None:
    """Apply presentation/workflow overrides without changing core route storage."""

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

        blocks.append(f"<b>{point['nav_address']}</b>")
        return "\n\n".join(blocks)

    async def send_current_card(message, route_id: int):
        """Send a fresh copy of the active route point to the bottom of the chat."""
        points = await bot_module.get_points(route_id)
        if not points:
            return
        idx = bot_module.first_pending(points)
        if idx is None:
            await message.answer(
                "🏁 <b>Все точки выполнены</b>\n\n" + bot_module.summary_text(points),
                parse_mode="HTML",
                reply_markup=bot_module.summary_kb(route_id),
            )
            return
        point = points[idx]
        await message.answer(
            point_text(point, idx, len(points)),
            parse_mode="HTML",
            reply_markup=bot_module.point_kb(point, route_id, idx, len(points)),
        )

    # Deadline warning gets its own callback so dismissing it can bring the
    # active application card back to the bottom of the conversation.
    def deadline_kb(route_id: int, point_id: int):
        return bot_module.InlineKeyboardMarkup(inline_keyboard=[
            [bot_module.InlineKeyboardButton(text="⬆️ Сделать следующей", callback_data=f"moveafter:{route_id}:{point_id}")],
            [bot_module.InlineKeyboardButton(text="↕️ Выбрать место", callback_data=f"move:{route_id}:{point_id}")],
            [bot_module.InlineKeyboardButton(text="Оставить как есть", callback_data=f"deadlinekeep:{route_id}")],
        ])

    original_movement_done = bot_module.movement_done

    async def movement_done(message, route_id: int, point_id: int, position: int):
        # Keep the existing confirmation / remember-order prompt, then always
        # place the active point card at the very bottom of the chat.
        await original_movement_done(message, route_id, point_id, position)
        await send_current_card(message, route_id)

    @bot_module.dp.callback_query(bot_module.F.data.startswith("deadlinekeep:"))
    async def deadline_keep(cb):
        route_id = int(cb.data.split(":")[1])
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer("Оставлено как есть")
        await send_current_card(cb.message, route_id)

    bot_module.point_text = point_text
    bot_module.deadline_kb = deadline_kb
    bot_module.movement_done = movement_done
