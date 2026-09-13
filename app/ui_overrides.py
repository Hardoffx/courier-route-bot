from __future__ import annotations

from collections import Counter
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity


def apply(bot_module) -> None:
    """Apply presentation overrides without changing route/storage logic."""

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

        lab_lines = [f"🟢 INVITRO: {invitro} · 🟡 CMD: {cmd}"]
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
        """HTML fallback used outside the native point-card handlers."""
        state = "✅ <b>ВЫПОЛНЕНО</b>\n" if point["done"] else "➡️ <b>ТЕКУЩАЯ ТОЧКА</b>\n"
        source = " · ➕ доп." if point["source"] == "MANUAL" else ""
        lab_name = other_lab_name(point)
        lab_badge = f"🟠 {lab_name}" if lab_name else bot_module.badge(point["lab_type"])
        blocks = [f"{state}<b>{index + 1} из {total}</b> · {lab_badge}{source}"]

        window = bot_module.window_text(point)
        if point.get("lab_type") == "CMD":
            if point.get("facility_code"):
                blocks.append(f"🏥 ЛПУ №<b>{point['facility_code']}</b>")
            if window:
                blocks.append(f"🕓 <b>{window}</b>")
        elif window:
            blocks.append(f"🕓 <b>{window}</b>")

        phone = bot_module.format_phone(point.get("phone"))
        if phone:
            blocks.append(f"📞 {phone}")
        if point.get("note"):
            blocks.append(f"📝 <b>Не забыть:</b> {point['note']}")
        blocks.append(f"<b>{point['nav_address']}</b>")
        return "\n\n".join(blocks)

    def point_kb(point, route_id, index, total):
        rows = [[InlineKeyboardButton(text="🗺 Яндекс.Карты", url=bot_module.yandex_url(point["nav_address"]))]]
        if not point["done"]:
            rows.append([InlineKeyboardButton(text="✅ Выполнено → следующая", callback_data=f"done:{point['id']}:{route_id}:{index}")])
        rows.append([
            InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{point['id']}:{route_id}:{index}"),
            InlineKeyboardButton(text="📞 Телефон", callback_data=f"phone:{point['id']}:{route_id}:{index}"),
        ])
        rows.append([
            InlineKeyboardButton(text="↕️ Переместить", callback_data=f"move:{route_id}:{point['id']}"),
            InlineKeyboardButton(text="➕ После этой", callback_data=f"add:{route_id}:{index}"),
        ])
        nav = []
        if index > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"point:{route_id}:{index-1}"))
        if index < total - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"point:{route_id}:{index+1}"))
        if nav:
            rows.append(nav)
        rows.append([
            InlineKeyboardButton(text="📋 К списку", callback_data=f"list:{route_id}:{index//6}"),
            InlineKeyboardButton(text="🏠 Маршрут", callback_data=f"summary:{route_id}"),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def u16len(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    def point_native(point, index, total):
        """Return plain text plus explicit Telegram entities.

        The phone is marked as a native phone_number entity. This makes the
        number clickable in Telegram clients, including Android, without a
        separate call button and without relying on HTML tel: handling.
        """
        parts: list[str] = []
        entities: list[MessageEntity] = []
        cursor = 0

        def append(text: str, entity_type: str | None = None):
            nonlocal cursor
            parts.append(text)
            length = u16len(text)
            if entity_type and length:
                entities.append(MessageEntity(type=entity_type, offset=cursor, length=length))
            cursor += length

        append("✅ " if point["done"] else "➡️ ")
        append("ВЫПОЛНЕНО" if point["done"] else "ТЕКУЩАЯ ТОЧКА", "bold")
        append("\n")
        append(f"{index + 1} из {total}", "bold")
        lab_name = other_lab_name(point)
        lab_badge = f"🟠 {lab_name}" if lab_name else bot_module.badge(point["lab_type"])
        source = " · ➕ доп." if point["source"] == "MANUAL" else ""
        append(f" · {lab_badge}{source}")

        window = bot_module.window_text(point)
        if point.get("lab_type") == "CMD" and point.get("facility_code"):
            append("\n\n🏥 ЛПУ №")
            append(str(point["facility_code"]), "bold")
        if window:
            append("\n\n🕓 ")
            append(str(window), "bold")

        phone = bot_module.format_phone(point.get("phone"))
        if phone:
            append("\n\n📞 ")
            append(phone, "phone_number")

        if point.get("note"):
            append("\n\n📝 ")
            append("Не забыть:", "bold")
            append(f" {point['note']}")

        append("\n\n")
        append(str(point["nav_address"]), "bold")
        return "".join(parts), entities

    async def edit_point(message, point, route_id, index, total):
        text, entities = point_native(point, index, total)
        await message.edit_text(
            text,
            entities=entities,
            reply_markup=point_kb(point, route_id, index, total),
        )

    async def answer_point(message, prefix, point, route_id, index, total):
        text, entities = point_native(point, index, total)
        if prefix:
            prefix_len = u16len(prefix)
            entities = [
                MessageEntity(type=e.type, offset=e.offset + prefix_len, length=e.length)
                for e in entities
            ]
            text = prefix + text
        await message.answer(
            text,
            entities=entities,
            reply_markup=point_kb(point, route_id, index, total),
        )

    async def resume_route(cb):
        route_id = int(cb.data.split(":")[1])
        points = await bot_module.get_points(route_id)
        if not points:
            await cb.answer("Маршрут пуст", show_alert=True)
            return
        idx = bot_module.first_pending(points)
        if idx is None:
            await cb.message.edit_text(
                "🏁 <b>Все точки выполнены</b>\n\n" + summary_text(points),
                parse_mode="HTML",
                reply_markup=bot_module.summary_kb(route_id),
            )
            await cb.answer()
            return
        await edit_point(cb.message, points[idx], route_id, idx, len(points))
        await cb.answer()

    async def show_point(cb):
        _, route_s, idx_s = cb.data.split(":")
        route_id, idx = int(route_s), int(idx_s)
        points = await bot_module.get_points(route_id)
        if not points:
            await cb.answer("Маршрут пуст", show_alert=True)
            return
        idx = max(0, min(idx, len(points) - 1))
        await edit_point(cb.message, points[idx], route_id, idx, len(points))
        await cb.answer()

    async def done(cb):
        _, point_s, route_s, _ = cb.data.split(":")
        route_id = int(route_s)
        await bot_module.mark_done(int(point_s))
        points = await bot_module.get_points(route_id)
        idx = bot_module.first_pending(points)
        if idx is None:
            await cb.message.edit_text(
                "🏁 <b>Маршрут завершён!</b>\n\n" + summary_text(points),
                parse_mode="HTML",
                reply_markup=bot_module.summary_kb(route_id),
            )
        else:
            await edit_point(cb.message, points[idx], route_id, idx, len(points))
        await cb.answer("✅ Выполнено")

    async def note_save(message, state):
        data = await state.get_data()
        await bot_module.set_note(data["point_id"], message.text.strip())
        point = await bot_module.get_point(data["point_id"])
        points = await bot_module.get_points(data["route_id"])
        await state.clear()
        await answer_point(message, "✅ Заметка сохранена.\n\n", point, data["route_id"], data["idx"], len(points))

    async def phone_save(message, state):
        data = await state.get_data()
        phone = await bot_module.set_phone(data["point_id"], message.text.strip())
        if not phone:
            await message.answer("❌ Не понял номер. Пришли российский номер из 10–11 цифр, например 8 495 123-45-67.")
            return
        point = await bot_module.get_point(data["point_id"])
        points = await bot_module.get_points(data["route_id"])
        await state.clear()
        await answer_point(message, "✅ Телефон сохранён за этим адресом\n\n", point, data["route_id"], data["idx"], len(points))

    async def add_save(message, state):
        data = await state.get_data()
        address = bot_module.normalize_address(message.text.strip())
        after = data.get("after_index", -1)
        position = after + 2 if after >= 0 else None
        point_id = await bot_module.add_manual_point(data["route_id"], address, position=position)
        await state.clear()
        points = await bot_module.get_points(data["route_id"])
        idx = next(i for i, p in enumerate(points) if p["id"] == point_id)
        await answer_point(message, "✅ Точка добавлена\n\n", points[idx], data["route_id"], idx, len(points))

    async def start(message):
        await message.answer(
            "🚚 <b>RoutePilot</b>\n\n"
            "Отправь скриншот маршрутного листа я распознаю точки и подготовлю маршрут для работы.",
            parse_mode="HTML",
        )

    bot_module.summary_text = summary_text
    bot_module.point_text = point_text
    bot_module.point_kb = point_kb

    callback_replacements = {
        bot_module.resume_route: resume_route,
        bot_module.show_point: show_point,
        bot_module.done: done,
    }
    for handler in bot_module.dp.callback_query.handlers:
        replacement = callback_replacements.get(getattr(handler, "callback", None))
        if replacement:
            handler.callback = replacement

    message_replacements = {
        bot_module.start: start,
        bot_module.note_save: note_save,
        bot_module.phone_save: phone_save,
        bot_module.add_save: add_save,
    }
    for handler in bot_module.dp.message.handlers:
        replacement = message_replacements.get(getattr(handler, "callback", None))
        if replacement:
            handler.callback = replacement
