from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from app.db import (
    add_manual_point,
    compare_routes,
    create_route,
    get_point,
    get_points,
    init_db,
    latest_route,
    mark_done,
    move_point,
    pending_deadline_alerts,
    save_route_order,
    set_note,
    set_phone,
)
from app.normalizer import normalize_address
from app.ocr import extract_rows

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
UPLOADS = Path("data/uploads")
UPLOADS.mkdir(parents=True, exist_ok=True)
MOSCOW = ZoneInfo("Europe/Moscow")
logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())


class NoteState(StatesGroup):
    waiting = State()


class AddState(StatesGroup):
    waiting_address = State()


class PhoneState(StatesGroup):
    waiting = State()


class MoveState(StatesGroup):
    waiting_position = State()


def badge(kind):
    return {"INVITRO": "🟢 INVITRO", "CMD": "🟡 CMD", "OTHER": "🟠 ДРУГИЕ", "UNKNOWN": "⚪ ?"}.get(kind, "⚪ ?")


def icon(kind):
    return {"INVITRO": "🟢", "CMD": "🟡", "OTHER": "🟠", "UNKNOWN": "⚪"}.get(kind, "⚪")


def yandex_url(address):
    return "https://yandex.ru/maps/?text=" + quote_plus(address)


def first_pending(points):
    return next((i for i, p in enumerate(points) if not p["done"]), None)


def format_phone(phone: str | None) -> str | None:
    if not phone or not phone.startswith("+7") or len(phone) != 12:
        return phone
    d = phone[2:]
    return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"


def window_text(point: dict) -> str | None:
    if point.get("window_start") and point.get("window_end"):
        return f"{point['window_start']}–{point['window_end']}"
    return point.get("window_end")


def summary_text(points, diff_text=None):
    counts = {k: sum(1 for p in points if p["lab_type"] == k) for k in ("INVITRO", "CMD", "OTHER", "UNKNOWN")}
    done = sum(int(p["done"]) for p in points)
    left = len(points) - done
    progress = f"\n\n✅ Выполнено: <b>{done}/{len(points)}</b> · осталось <b>{left}</b>" if done else ""
    diff = f"\n\n{diff_text}" if diff_text else ""
    return (
        f"🚚 <b>Маршрут</b>\n\nТочек: <b>{len(points)}</b>\n"
        f"🟢 INVITRO: {counts['INVITRO']} · 🟡 CMD: {counts['CMD']}\n"
        f"🟠 Другие: {counts['OTHER']} · ⚪ ?: {counts['UNKNOWN']}{progress}{diff}"
    )


def summary_kb(route_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ К текущей точке", callback_data=f"resume:{route_id}")],
        [
            InlineKeyboardButton(text="📋 Весь маршрут", callback_data=f"list:{route_id}:0"),
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"add:{route_id}:-1"),
        ],
    ])


def point_kb(point, route_id, index, total):
    rows = [[InlineKeyboardButton(text="🗺 Яндекс.Карты", url=yandex_url(point["nav_address"]))]]
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
    state = "✅ <b>ВЫПОЛНЕНО</b>\n" if point["done"] else "➡️ <b>ТЕКУЩАЯ ТОЧКА</b>\n"
    source = " · ➕ доп." if point["source"] == "MANUAL" else ""
    note = f"\n\n📝 <b>Не забыть:</b> {point['note']}" if point["note"] else ""
    extra = []
    if point.get("lab_type") == "CMD" and point.get("facility_code"):
        extra.append(f"🏥 ЛПУ №<b>{point['facility_code']}</b>")
    window = window_text(point)
    if window:
        extra.append(f"🕓 Забор: <b>{window}</b>")
    phone = format_phone(point.get("phone"))
    if phone:
        extra.append(f"📞 {phone}")
    details = ("\n" + "\n".join(extra)) if extra else ""
    return (
        f"{state}<b>{index+1} из {total}</b> · {badge(point['lab_type'])}{source}\n\n"
        f"<b>{point['nav_address']}</b>{details}{note}"
    )


def list_keyboard(route_id, page, start, chunk, total):
    rows = []
    numbered = []
    for offset, p in enumerate(chunk):
        idx = start + offset
        numbered.append(InlineKeyboardButton(text=("✓ " if p["done"] else "") + str(idx + 1), callback_data=f"point:{route_id}:{idx}"))
        if len(numbered) == 3:
            rows.append(numbered)
            numbered = []
    if numbered:
        rows.append(numbered)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"list:{route_id}:{page-1}"))
    if start + len(chunk) < total:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"list:{route_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="▶️ К текущей", callback_data=f"resume:{route_id}"),
        InlineKeyboardButton(text="🏠 Маршрут", callback_data=f"summary:{route_id}"),
    ])
    rows.append([InlineKeyboardButton(text="➕ Добавить в конец", callback_data=f"add:{route_id}:-1")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def move_menu(route_id: int, point_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬆️ Сделать следующей", callback_data=f"moveafter:{route_id}:{point_id}")],
        [
            InlineKeyboardButton(text="⬆️ На 1 выше", callback_data=f"movestep:{route_id}:{point_id}:-1"),
            InlineKeyboardButton(text="⬇️ На 1 ниже", callback_data=f"movestep:{route_id}:{point_id}:1"),
        ],
        [InlineKeyboardButton(text="🔢 Выбрать позицию", callback_data=f"movepos:{route_id}:{point_id}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="moveclose")],
    ])


def remember_kb(route_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Запомнить такой порядок", callback_data=f"remember:{route_id}")],
        [InlineKeyboardButton(text="☀️ Только сегодня", callback_data="moveclose")],
    ])


def deadline_kb(route_id: int, point_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬆️ Сделать следующей", callback_data=f"moveafter:{route_id}:{point_id}")],
        [InlineKeyboardButton(text="↕️ Выбрать место", callback_data=f"move:{route_id}:{point_id}")],
        [InlineKeyboardButton(text="Оставить как есть", callback_data="moveclose")],
    ])


async def movement_done(message: Message, route_id: int, point_id: int, position: int):
    point = await get_point(point_id)
    await message.answer(
        f"✅ <b>Точка перемещена на №{position}</b>\n{point['nav_address']}\n\n"
        "Сохранить этот порядок для следующих похожих маршрутов?",
        parse_mode="HTML",
        reply_markup=remember_kb(route_id),
    )


@dp.message(CommandStart())
async def start(message):
    await message.answer(
        "🚚 <b>RoutePilot</b>\n\nОтправь полный скриншот маршрутного листа. "
        "Склад и Прядильную пропущу, остальные точки подготовлю для работы.",
        parse_mode="HTML",
    )


@dp.message(Command("route"))
async def route_cmd(message):
    route = await latest_route(message.chat.id)
    if not route:
        await message.answer("Маршрутов пока нет. Отправь утреннюю фотографию.")
        return
    points = await get_points(route["id"])
    await message.answer(summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route["id"]))


@dp.message(Command("add"))
async def add_cmd(message, state):
    route = await latest_route(message.chat.id)
    if not route:
        await message.answer("Сначала отправь утреннюю фотографию.")
        return
    await state.set_state(AddState.waiting_address)
    await state.update_data(route_id=route["id"], after_index=-1)
    await message.answer("➕ Пришли дополнительный адрес текстом.")


@dp.message(F.photo)
async def photo(message, bot):
    status = await message.answer("🔎 Распознаю маршрут, ЛПУ и время…")
    path = UPLOADS / f"{message.chat.id}_{message.message_id}.jpg"
    await bot.download(message.photo[-1], destination=path)
    try:
        rows = await asyncio.to_thread(extract_rows, str(path))
    except Exception as exc:
        logging.exception("OCR failed")
        await status.edit_text(f"❌ Ошибка распознавания: {exc}")
        return
    if not rows:
        await status.edit_text("❌ Не удалось найти адресные строки.")
        return
    payload = [
        {
            "raw_text": r.raw_text,
            "nav_address": r.nav_address,
            "lab_type": r.lab_type,
            "facility_code": r.facility_code,
            "window_start": r.window_start,
            "window_end": r.window_end,
            "source": "PHOTO",
        }
        for r in rows
    ]
    today = datetime.now(MOSCOW).date().isoformat()
    route_id = await create_route(message.chat.id, today, str(path), payload)
    points = await get_points(route_id)
    previous = await latest_route(message.chat.id, exclude_route_id=route_id)
    diff_text = "Первый сохранённый маршрут."
    if previous:
        diff = compare_routes(await get_points(previous["id"]), points)
        if diff["same"]:
            diff_text = "✅ Совпадает с предыдущим маршрутом."
        else:
            chunks = ["🔄 Маршрут изменился."]
            if diff["added"]:
                chunks.append(f"➕ Добавлено: {len(diff['added'])}")
            if diff["removed"]:
                chunks.append(f"➖ Убрано: {len(diff['removed'])}")
            if diff["order_changed"]:
                chunks.append("↕️ Изменён порядок")
            diff_text = "\n".join(chunks)
    cmd_codes = sum(1 for p in points if p["lab_type"] == "CMD" and p.get("facility_code"))
    windows = sum(1 for p in points if p.get("window_end"))
    diff_text += f"\n🏥 CMD с № ЛПУ: {cmd_codes} · 🕓 окон времени: {windows}"
    await status.edit_text(summary_text(points, diff_text), parse_mode="HTML", reply_markup=summary_kb(route_id))


@dp.callback_query(F.data.startswith("summary:"))
async def show_summary(cb):
    route_id = int(cb.data.split(":")[1])
    points = await get_points(route_id)
    await cb.message.edit_text(summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route_id))
    await cb.answer()


@dp.callback_query(F.data.startswith("resume:"))
async def resume_route(cb):
    route_id = int(cb.data.split(":")[1])
    points = await get_points(route_id)
    if not points:
        await cb.answer("Маршрут пуст", show_alert=True)
        return
    idx = first_pending(points)
    if idx is None:
        await cb.message.edit_text("🏁 <b>Все точки выполнены</b>\n\n" + summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route_id))
        await cb.answer()
        return
    p = points[idx]
    await cb.message.edit_text(point_text(p, idx, len(points)), parse_mode="HTML", reply_markup=point_kb(p, route_id, idx, len(points)))
    await cb.answer()


@dp.callback_query(F.data.startswith("point:"))
async def show_point(cb):
    _, route_s, idx_s = cb.data.split(":")
    route_id, idx = int(route_s), int(idx_s)
    points = await get_points(route_id)
    if not points:
        await cb.answer("Маршрут пуст", show_alert=True)
        return
    idx = max(0, min(idx, len(points) - 1))
    p = points[idx]
    await cb.message.edit_text(point_text(p, idx, len(points)), parse_mode="HTML", reply_markup=point_kb(p, route_id, idx, len(points)))
    await cb.answer()


@dp.callback_query(F.data.startswith("done:"))
async def done(cb):
    _, point_s, route_s, _ = cb.data.split(":")
    route_id = int(route_s)
    await mark_done(int(point_s))
    points = await get_points(route_id)
    idx = first_pending(points)
    if idx is None:
        await cb.message.edit_text("🏁 <b>Маршрут завершён!</b>\n\n" + summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route_id))
    else:
        p = points[idx]
        await cb.message.edit_text(point_text(p, idx, len(points)), parse_mode="HTML", reply_markup=point_kb(p, route_id, idx, len(points)))
    await cb.answer("✅ Выполнено")


@dp.callback_query(F.data.startswith("list:"))
async def list_points(cb):
    _, route_s, page_s = cb.data.split(":")
    route_id, page = int(route_s), int(page_s)
    points = await get_points(route_id)
    per_page = 6
    max_page = max(0, (len(points) - 1) // per_page)
    page = max(0, min(page, max_page))
    start = page * per_page
    chunk = points[start:start + per_page]
    end = start + len(chunk)
    current = first_pending(points)
    lines = [f"📋 <b>Маршрут · {len(points)} точек</b>   <i>{start+1}–{end}</i>\n"]
    for i, p in enumerate(chunk, start=start):
        mark = "✅" if p["done"] else ("➡️" if i == current else icon(p["lab_type"]))
        note = " 📝" if p["note"] else ""
        code = f" · ЛПУ {p['facility_code']}" if p["lab_type"] == "CMD" and p.get("facility_code") else ""
        end_time = f" · до {p['window_end']}" if p.get("window_end") else ""
        lines.append(f"<b>{i+1}.</b> {mark}{note} {p['nav_address']}{code}{end_time}")
    if current is not None:
        lines.append(f"\n➡️ Текущая: <b>№{current+1}</b>")
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=list_keyboard(route_id, page, start, chunk, len(points)))
    await cb.answer()


@dp.callback_query(F.data.startswith("note:"))
async def note_begin(cb, state):
    _, point_s, route_s, idx_s = cb.data.split(":")
    await state.set_state(NoteState.waiting)
    await state.update_data(point_id=int(point_s), route_id=int(route_s), idx=int(idx_s))
    await cb.message.answer("📝 Напиши заметку: что взять, передать или забрать.")
    await cb.answer()


@dp.message(NoteState.waiting)
async def note_save(message, state):
    data = await state.get_data()
    await set_note(data["point_id"], message.text.strip())
    point = await get_point(data["point_id"])
    points = await get_points(data["route_id"])
    await state.clear()
    await message.answer("✅ Заметка сохранена.\n\n" + point_text(point, data["idx"], len(points)), parse_mode="HTML", reply_markup=point_kb(point, data["route_id"], data["idx"], len(points)))


@dp.callback_query(F.data.startswith("phone:"))
async def phone_begin(cb, state):
    _, point_s, route_s, idx_s = cb.data.split(":")
    point = await get_point(int(point_s))
    current = f"\nСейчас: {format_phone(point.get('phone'))}" if point and point.get("phone") else ""
    await state.set_state(PhoneState.waiting)
    await state.update_data(point_id=int(point_s), route_id=int(route_s), idx=int(idx_s))
    await cb.message.answer("📞 Пришли телефон ЛПУ. Можно с 8, +7 или просто 10 цифр — сохраню в формате +7." + current)
    await cb.answer()


@dp.message(PhoneState.waiting)
async def phone_save(message, state):
    data = await state.get_data()
    phone = await set_phone(data["point_id"], message.text.strip())
    if not phone:
        await message.answer("❌ Не понял номер. Пришли российский номер из 10–11 цифр, например 8 495 123-45-67.")
        return
    point = await get_point(data["point_id"])
    points = await get_points(data["route_id"])
    await state.clear()
    await message.answer(
        f"✅ Телефон сохранён за этим адресом: {format_phone(phone)}\n\n" + point_text(point, data["idx"], len(points)),
        parse_mode="HTML",
        reply_markup=point_kb(point, data["route_id"], data["idx"], len(points)),
    )


@dp.callback_query(F.data.startswith("add:"))
async def add_begin(cb, state):
    _, route_s, after_s = cb.data.split(":")
    after = int(after_s)
    await state.set_state(AddState.waiting_address)
    await state.update_data(route_id=int(route_s), after_index=after)
    where = "после текущей точки" if after >= 0 else "в конец маршрута"
    await cb.message.answer(f"➕ Пришли дополнительный адрес текстом.\nДобавлю <b>{where}</b>.", parse_mode="HTML")
    await cb.answer()


@dp.message(AddState.waiting_address)
async def add_save(message, state):
    data = await state.get_data()
    address = normalize_address(message.text.strip())
    after = data.get("after_index", -1)
    position = after + 2 if after >= 0 else None
    point_id = await add_manual_point(data["route_id"], address, position=position)
    await state.clear()
    points = await get_points(data["route_id"])
    idx = next(i for i, p in enumerate(points) if p["id"] == point_id)
    point = points[idx]
    await message.answer("✅ <b>Точка добавлена</b>\n\n" + point_text(point, idx, len(points)), parse_mode="HTML", reply_markup=point_kb(point, data["route_id"], idx, len(points)))


@dp.callback_query(F.data.startswith("move:"))
async def move_begin(cb):
    _, route_s, point_s = cb.data.split(":")
    point = await get_point(int(point_s))
    if not point:
        await cb.answer("Точка не найдена", show_alert=True)
        return
    await cb.message.answer(f"↕️ <b>Куда переместить?</b>\n{point['nav_address']}", parse_mode="HTML", reply_markup=move_menu(int(route_s), int(point_s)))
    await cb.answer()


@dp.callback_query(F.data.startswith("moveafter:"))
async def move_after_current(cb):
    _, route_s, point_s = cb.data.split(":")
    route_id, point_id = int(route_s), int(point_s)
    points = await get_points(route_id)
    current = first_pending(points)
    if current is None:
        await cb.answer("Все точки уже выполнены", show_alert=True)
        return
    target = next((p for p in points if p["id"] == point_id), None)
    if not target:
        await cb.answer("Точка не найдена", show_alert=True)
        return
    current_point = points[current]
    new_position = current_point["position"] + (0 if target["id"] == current_point["id"] else 1)
    pos = await move_point(route_id, point_id, new_position)
    await movement_done(cb.message, route_id, point_id, pos)
    await cb.answer("Перемещено")


@dp.callback_query(F.data.startswith("movestep:"))
async def move_step(cb):
    _, route_s, point_s, delta_s = cb.data.split(":")
    route_id, point_id, delta = int(route_s), int(point_s), int(delta_s)
    point = await get_point(point_id)
    if not point:
        await cb.answer("Точка не найдена", show_alert=True)
        return
    pos = await move_point(route_id, point_id, point["position"] + delta)
    await movement_done(cb.message, route_id, point_id, pos)
    await cb.answer("Перемещено")


@dp.callback_query(F.data.startswith("movepos:"))
async def move_choose_position(cb, state):
    _, route_s, point_s = cb.data.split(":")
    points = await get_points(int(route_s))
    await state.set_state(MoveState.waiting_position)
    await state.update_data(route_id=int(route_s), point_id=int(point_s))
    await cb.message.answer(f"🔢 Введи новое место точки: от 1 до {len(points)}.")
    await cb.answer()


@dp.message(MoveState.waiting_position)
async def move_position_save(message, state):
    data = await state.get_data()
    try:
        requested = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("Введи только номер позиции, например 7.")
        return
    points = await get_points(data["route_id"])
    if not 1 <= requested <= len(points):
        await message.answer(f"Нужен номер от 1 до {len(points)}.")
        return
    pos = await move_point(data["route_id"], data["point_id"], requested)
    await state.clear()
    await movement_done(message, data["route_id"], data["point_id"], pos)


@dp.callback_query(F.data.startswith("remember:"))
async def remember_order(cb):
    route_id = int(cb.data.split(":")[1])
    profile = await save_route_order(route_id)
    label = "выходных" if profile == "weekend" else "будней"
    await cb.message.edit_text(f"💾 Порядок сохранён для <b>{label}</b>. Следующие маршруты с теми же адресами будут собираться по нему.", parse_mode="HTML")
    await cb.answer("Порядок запомнен")


@dp.callback_query(F.data == "moveclose")
async def move_close(cb):
    with suppress(Exception):
        await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()


async def deadline_watcher(bot: Bot):
    while True:
        try:
            now = datetime.now(MOSCOW)
            for alert in await pending_deadline_alerts(now, minutes=40):
                code = f" · ЛПУ №{alert['facility_code']}" if alert.get("facility_code") else ""
                await bot.send_message(
                    alert["chat_id"],
                    "⚠️ <b>Скоро закончится время забора</b>\n\n"
                    f"Точка №{alert['position']}{code}\n"
                    f"<b>{alert['nav_address']}</b>\n"
                    f"До {alert['window_end']} — <b>{alert['minutes_left']} мин.</b>\n\n"
                    "Можно сразу поднять эту точку ближе.",
                    parse_mode="HTML",
                    reply_markup=deadline_kb(alert["route_id"], alert["id"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Deadline watcher failed")
        await asyncio.sleep(60)


async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    await init_db()
    bot = Bot(TOKEN)
    watcher = asyncio.create_task(deadline_watcher(bot))
    try:
        await dp.start_polling(bot)
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
