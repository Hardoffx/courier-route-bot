from __future__ import annotations

import asyncio
from collections import Counter
from html import escape
import re

import aiohttp
from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.optimizer import optimize_remaining_points_live


def apply(bot_module) -> None:
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

    def summary_kb(route_id):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ К текущей точке", callback_data=f"resume:{route_id}")],
            [InlineKeyboardButton(text="⚡ Оптимизировать по времени", callback_data=f"optimize:{route_id}")],
            [
                InlineKeyboardButton(text="📋 Весь маршрут", callback_data=f"list:{route_id}:0"),
                InlineKeyboardButton(text="➕ Добавить", callback_data=f"add:{route_id}:-1"),
            ],
        ])

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

    def point_text(point, index, total):
        state = "✅ <b>ВЫПОЛНЕНО</b>" if point["done"] else "➡️ <b>ТЕКУЩАЯ ТОЧКА</b>"
        source = " · ➕ доп." if point["source"] == "MANUAL" else ""
        lab_name = other_lab_name(point)
        lab_badge = f"🟠 {escape(lab_name)}" if lab_name else bot_module.badge(point["lab_type"])
        html = state + "<br>" + f"<b>{index + 1} из {total}</b> · {lab_badge}{source}"
        blocks = []
        window = bot_module.window_text(point)
        if point.get("lab_type") == "CMD":
            if point.get("facility_code"):
                blocks.append(f"🏥 ЛПУ №<b>{escape(str(point['facility_code']))}</b>")
            if window:
                blocks.append(f"🕓 <b>{escape(str(window))}</b>")
        elif window:
            blocks.append(f"🕓 <b>{escape(str(window))}</b>")
        phone_raw = point.get("phone")
        phone = bot_module.format_phone(phone_raw)
        if phone and phone_raw:
            blocks.append(f'📞 <a href="tel:{escape(str(phone_raw), quote=True)}">{escape(str(phone))}</a>')
        if point.get("note"):
            blocks.append(f"📝 <b>Не забыть:</b> {escape(str(point['note']))}")
        blocks.append(f"<b>{escape(str(point['nav_address']))}</b>")
        if blocks:
            html += "<br><br>" + "<br><br>".join(blocks)
        return html

    def time_label(minutes: int | None) -> str | None:
        if minutes is None:
            return None
        minutes = max(0, int(minutes))
        hours, mins = divmod(minutes, 60)
        if hours:
            human = f"{hours} ч {mins} мин" if mins else f"{hours} ч"
            return f"{minutes} мин ({human})"
        return f"{minutes} мин"

    def timing_lines(result, remaining_count: int) -> str:
        if result.estimated_drive_minutes is None:
            return ""
        lines = [f"🚗 В дороге: <b>≈{time_label(result.estimated_drive_minutes)}</b>"]
        if result.estimated_service_minutes is not None:
            lines.append(f"📦 На точках: <b>≈{time_label(result.estimated_service_minutes)}</b> (по 7 мин × {remaining_count})")
        if result.estimated_wait_minutes:
            lines.append(f"⏳ Ожидание открытия: <b>≈{time_label(result.estimated_wait_minutes)}</b>")
        if result.estimated_total_minutes is not None:
            lines.append(f"⏱ Всего по расчёту: <b>≈{time_label(result.estimated_total_minutes)}</b>")
        return "\n" + "\n".join(lines)

    async def bot_api(method: str, payload: dict):
        url = f"https://api.telegram.org/bot{bot_module.TOKEN}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', 'unknown error')}")
        return data.get("result")

    def markup_json(markup):
        return markup.model_dump(exclude_none=True) if markup else None

    async def edit_point(message, point, route_id, index, total):
        await bot_api("editMessageText", {
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "rich_message": {"html": point_text(point, index, total)},
            "reply_markup": markup_json(point_kb(point, route_id, index, total)),
        })

    async def answer_point(message, prefix, point, route_id, index, total):
        prefix_html = escape(prefix).replace("\n", "<br>") if prefix else ""
        await bot_api("sendRichMessage", {
            "chat_id": message.chat.id,
            "rich_message": {"html": prefix_html + point_text(point, index, total)},
            "reply_markup": markup_json(point_kb(point, route_id, index, total)),
        })

    async def resume_route(cb):
        route_id = int(cb.data.split(":")[1])
        points = await bot_module.get_points(route_id)
        if not points:
            await cb.answer("Маршрут пуст", show_alert=True)
            return
        idx = bot_module.first_pending(points)
        if idx is None:
            await cb.message.edit_text("🏁 <b>Все точки выполнены</b>\n\n" + summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route_id))
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
            await cb.message.edit_text("🏁 <b>Маршрут завершён!</b>\n\n" + summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route_id))
        else:
            await edit_point(cb.message, points[idx], route_id, idx, len(points))
        await cb.answer("✅ Выполнено")

    async def optimize_route(cb):
        route_id = int(cb.data.split(":")[1])
        points = await bot_module.get_points(route_id)
        if not points:
            await cb.answer("Маршрут пуст", show_alert=True)
            return

        await cb.answer()
        # Edit the route card itself so progress is always visible, even on clients
        # that hide short-lived callback notifications or delay a new bot message.
        await cb.message.edit_text(
            "⏳ <b>Оптимизирую маршрут…</b>\n\n"
            "Строю дорожную матрицу между адресами.\n"
            "Проверяю время работы ЛПУ и ищу лучший порядок точек.\n\n"
            "<i>Расчёт выполняется, дождись результата…</i>",
            parse_mode="HTML",
            reply_markup=None,
        )

        async def typing_loop():
            try:
                while True:
                    await bot_module.bot.send_chat_action(chat_id=cb.message.chat.id, action="typing")
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        typing_task = asyncio.create_task(typing_loop())
        try:
            result = await optimize_remaining_points_live(points)
        except Exception:
            typing_task.cancel()
            await cb.message.edit_text(
                "❌ <b>Не удалось закончить оптимизацию.</b>\n\nПопробуй нажать кнопку ещё раз.",
                parse_mode="HTML",
                reply_markup=summary_kb(route_id),
            )
            return
        finally:
            typing_task.cancel()

        remaining_count = sum(1 for p in points if not p.get("done"))
        if result.mode == "road":
            if result.geocoded == remaining_count:
                mode_line = f"🚗 Дорожная матрица: <b>{result.geocoded}/{remaining_count}</b> адресов."
            else:
                mode_line = f"🚗 Дорожная матрица: <b>{result.geocoded}/{remaining_count}</b> адресов. Для неопознанных адресов использована консервативная оценка."
        else:
            mode_line = f"🧭 Дорожная матрица недоступна · координаты: <b>{result.geocoded}/{remaining_count}</b>. Использован резервный расчёт по районам."

        times = timing_lines(result, remaining_count)
        if result.moved:
            for position, point_id in enumerate(result.ordered_ids, 1):
                await bot_module.move_point(route_id, point_id, position)
            updated = await bot_module.get_points(route_id)
            note = (
                "⚡ <b>Маршрут перестроен</b>\n"
                f"{mode_line}\n"
                f"Изменено позиций: <b>{result.moved}</b> · риск по времени: <b>{len(result.urgent_ids)}</b>."
                f"{times}"
            )
            await cb.message.edit_text(summary_text(updated, note), parse_mode="HTML", reply_markup=summary_kb(route_id))
        else:
            note = (
                "⚡ <b>Проверил весь оставшийся маршрут</b>\n"
                f"{mode_line}\n"
                "Переставлять точки сейчас невыгодно: исходный порядок уже хороший с учётом времени ЛПУ."
                f"{times}"
            )
            await cb.message.edit_text(summary_text(points, note), parse_mode="HTML", reply_markup=summary_kb(route_id))

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
    bot_module.summary_kb = summary_kb
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

    bot_module.dp.callback_query.register(optimize_route, F.data.startswith("optimize:"))
