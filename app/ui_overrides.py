from __future__ import annotations

from collections import Counter
import re

from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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

        if point.get("note"):
            blocks.append(f"📝 <b>Не забыть:</b> {point['note']}")

        blocks.append(f"<b>{point['nav_address']}</b>")
        return "\n\n".join(blocks)

    def point_kb(point, route_id, index, total):
        rows = [[InlineKeyboardButton(text="🗺 Яндекс.Карты", url=bot_module.yandex_url(point["nav_address"]))]]
        if not point["done"]:
            rows.append([InlineKeyboardButton(text="✅ Выполнено → следующая", callback_data=f"done:{point['id']}:{route_id}:{index}")])

        if point.get("phone"):
            phone_label = bot_module.format_phone(point.get("phone")) or point.get("phone")
            rows.append([
                InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{point['id']}:{route_id}:{index}"),
                InlineKeyboardButton(text=f"📞 {phone_label}", callback_data=f"call:{point['id']}"),
            ])
            rows.append([InlineKeyboardButton(text="✏️ Изменить номер", callback_data=f"phone:{point['id']}:{route_id}:{index}")])
        else:
            rows.append([
                InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{point['id']}:{route_id}:{index}"),
                InlineKeyboardButton(text="📞 Добавить телефон", callback_data=f"phone:{point['id']}:{route_id}:{index}"),
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

    async def call_phone(cb):
        point_id = int(cb.data.split(":", 1)[1])
        point = await bot_module.get_point(point_id)
        if not point or not point.get("phone"):
            await cb.answer("Телефон не найден", show_alert=True)
            return
        name = (point.get("nav_address") or "ЛПУ").strip()[:64]
        await cb.message.answer_contact(
            phone_number=point["phone"],
            first_name=name or "ЛПУ",
        )
        await cb.answer("Открываю номер")

    async def start(message):
        await message.answer(
            "🚚 <b>RoutePilot</b>\n\n"
            "Отправь скриншот маршрутного листа я распознаю точки и подготовлю маршрут для работы.",
            parse_mode="HTML",
        )

    bot_module.summary_text = summary_text
    bot_module.point_text = point_text
    bot_module.point_kb = point_kb

    # /start handler is already registered when app.bot is imported. Replace
    # its callback in-place so the presentation override takes effect without
    # touching the main route logic.
    for handler in bot_module.dp.message.handlers:
        if getattr(handler, "callback", None) is bot_module.start:
            handler.callback = start
            break

    # A native Telegram contact card is reliable on Android and gives the user
    # an actual tappable phone action even when tel: links in ordinary HTML
    # messages are not rendered by the client.
    bot_module.dp.callback_query.register(call_phone, F.data.startswith("call:"))
