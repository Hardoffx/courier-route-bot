from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

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
    set_note,
)
from app.normalizer import normalize_address
from app.ocr import extract_rows

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
UPLOADS = Path("data/uploads")
UPLOADS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())


class NoteState(StatesGroup):
    waiting = State()


class AddState(StatesGroup):
    waiting = State()


def badge(kind: str) -> str:
    return {"INVITRO": "🟢 INVITRO", "CMD": "🟡 CMD", "OTHER": "🟠 ДРУГИЕ", "UNKNOWN": "⚪ ?"}.get(kind, "⚪ ?")


def icon(kind: str) -> str:
    return {"INVITRO": "🟢", "CMD": "🟡", "OTHER": "🟠", "UNKNOWN": "⚪"}.get(kind, "⚪")


def yandex_url(address: str) -> str:
    return "https://yandex.ru/maps/?text=" + quote_plus(address)


def summary_text(points: list[dict], diff_text: str | None = None) -> str:
    counts = {k: sum(1 for p in points if p["lab_type"] == k) for k in ("INVITRO", "CMD", "OTHER", "UNKNOWN")}
    done_count = sum(int(p["done"]) for p in points)
    progress = f"\n✅ Выполнено: <b>{done_count}/{len(points)}</b>" if done_count else ""
    diff = f"\n\n{diff_text}" if diff_text else ""
    return (
        "🚚 <b>Маршрут готов</b>\n\n"
        f"Точек: <b>{len(points)}</b>{progress}\n"
        f"🟢 INVITRO: {counts['INVITRO']}\n"
        f"🟡 CMD: {counts['CMD']}\n"
        f"🟠 Другие: {counts['OTHER']}\n"
        f"⚪ Не определено: {counts['UNKNOWN']}"
        f"{diff}"
    )


def summary_kb(route_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать / продолжить", callback_data=f"resume:{route_id}")],
        [
            InlineKeyboardButton(text="📋 Все точки", callback_data=f"list:{route_id}:0"),
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"add:{route_id}"),
        ],
    ])


def point_kb(point: dict, route_id: int, index: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🗺 Открыть в Яндекс.Картах", url=yandex_url(point["nav_address"]))],
        [
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{point['id']}:{route_id}:{index}"),
            InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{point['id']}:{route_id}:{index}"),
        ],
    ]
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Предыдущая", callback_data=f"point:{route_id}:{index-1}"))
    if index < total - 1:
        nav.append(InlineKeyboardButton(text="Следующая ➡️", callback_data=f"point:{route_id}:{index+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="📋 Весь список", callback_data=f"list:{route_id}:{index // 6}"),
        InlineKeyboardButton(text="🏠 Маршрут", callback_data=f"summary:{route_id}"),
    ])
    rows.append([InlineKeyboardButton(text="➕ Добавить точку", callback_data=f"add:{route_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def point_text(point: dict, index: int, total: int) -> str:
    source = " · ➕ доп." if point["source"] == "MANUAL" else ""
    note = f"\n\n⚠️ <b>Не забыть:</b> {point['note']}" if point["note"] else ""
    state = "✅ ВЫПОЛНЕНО\n" if point["done"] else ""
    return (
        f"{state}<b>ТОЧКА {index+1} / {total}</b> · {badge(point['lab_type'])}{source}\n\n"
        f"<b>{point['nav_address']}</b>{note}"
    )


def list_keyboard(route_id: int, page: int, start: int, chunk: list[dict], total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    numbered: list[InlineKeyboardButton] = []
    for offset, _ in enumerate(chunk):
        idx = start + offset
        numbered.append(InlineKeyboardButton(text=str(idx + 1), callback_data=f"point:{route_id}:{idx}"))
        if len(numbered) == 3:
            rows.append(numbered)
            numbered = []
    if numbered:
        rows.append(numbered)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"list:{route_id}:{page-1}"))
    if start + len(chunk) < total:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"list:{route_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="▶️ Продолжить", callback_data=f"resume:{route_id}"),
        InlineKeyboardButton(text="🏠 Маршрут", callback_data=f"summary:{route_id}"),
    ])
    rows.append([InlineKeyboardButton(text="➕ Добавить точку", callback_data=f"add:{route_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🚚 <b>RoutePilot</b>\n\nОтправь фотографию сегодняшнего маршрутного листа. "
        "Я возьму точки строго сверху вниз и подготовлю маршрут.",
        parse_mode="HTML",
    )


@dp.message(Command("route"))
async def route_cmd(message: Message):
    route = await latest_route(message.chat.id)
    if not route:
        await message.answer("Маршрутов пока нет. Отправь утреннюю фотографию.")
        return
    points = await get_points(route["id"])
    await message.answer(summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route["id"]))


@dp.message(Command("add"))
async def add_cmd(message: Message, state: FSMContext):
    route = await latest_route(message.chat.id)
    if not route:
        await message.answer("Сначала отправь утреннюю фотографию.")
        return
    await state.set_state(AddState.waiting)
    await state.update_data(route_id=route["id"])
    await message.answer("➕ Пришли дополнительный адрес текстом.")


@dp.message(F.photo)
async def photo(message: Message, bot: Bot):
    status = await message.answer("🔎 Распознаю маршрут…")
    path = UPLOADS / f"{message.chat.id}_{message.message_id}.jpg"
    await bot.download(message.photo[-1], destination=path)
    try:
        rows = await asyncio.to_thread(extract_rows, str(path))
    except Exception as exc:
        logging.exception("OCR failed")
        await status.edit_text(f"❌ Ошибка распознавания: {exc}")
        return
    if not rows:
        await status.edit_text("❌ Не удалось найти адресные строки. Попробуй отправить исходную картинку без обрезки/пересжатия.")
        return

    payload = [{"raw_text": r.raw_text, "nav_address": r.nav_address, "lab_type": r.lab_type, "source": "PHOTO"} for r in rows]
    route_id = await create_route(message.chat.id, date.today().isoformat(), str(path), payload)
    points = await get_points(route_id)
    previous = await latest_route(message.chat.id, exclude_route_id=route_id)
    diff_text = "Первый сохранённый маршрут."
    if previous:
        old_points = await get_points(previous["id"])
        diff = compare_routes(old_points, points)
        if diff["same"]:
            diff_text = "✅ Маршрут полностью совпадает с предыдущим."
        else:
            chunks = ["🔄 Маршрут изменился."]
            if diff["added"]:
                chunks.append(f"➕ Добавлено: {len(diff['added'])}")
            if diff["removed"]:
                chunks.append(f"➖ Убрано: {len(diff['removed'])}")
            if diff["order_changed"]:
                chunks.append("↕️ Изменён порядок точек")
            diff_text = "\n".join(chunks)

    await status.edit_text(
        summary_text(points, diff_text) + "\n\nПеред стартом можно быстро проверить список.",
        parse_mode="HTML",
        reply_markup=summary_kb(route_id),
    )


@dp.callback_query(F.data.startswith("summary:"))
async def show_summary(cb: CallbackQuery):
    route_id = int(cb.data.split(":")[1])
    points = await get_points(route_id)
    await cb.message.edit_text(summary_text(points), parse_mode="HTML", reply_markup=summary_kb(route_id))
    await cb.answer()


@dp.callback_query(F.data.startswith("resume:"))
async def resume_route(cb: CallbackQuery):
    route_id = int(cb.data.split(":")[1])
    points = await get_points(route_id)
    if not points:
        await cb.answer("Маршрут пуст", show_alert=True)
        return
    idx = next((i for i, p in enumerate(points) if not p["done"]), len(points) - 1)
    point = points[idx]
    await cb.message.edit_text(point_text(point, idx, len(points)), parse_mode="HTML", reply_markup=point_kb(point, route_id, idx, len(points)))
    await cb.answer()


@dp.callback_query(F.data.startswith("point:"))
async def show_point(cb: CallbackQuery):
    _, route_s, idx_s = cb.data.split(":")
    route_id, idx = int(route_s), int(idx_s)
    points = await get_points(route_id)
    if not points:
        await cb.answer("Маршрут пуст", show_alert=True)
        return
    idx = max(0, min(idx, len(points) - 1))
    point = points[idx]
    await cb.message.edit_text(point_text(point, idx, len(points)), parse_mode="HTML", reply_markup=point_kb(point, route_id, idx, len(points)))
    await cb.answer()


@dp.callback_query(F.data.startswith("done:"))
async def done(cb: CallbackQuery):
    _, point_s, route_s, idx_s = cb.data.split(":")
    point_id, route_id, idx = int(point_s), int(route_s), int(idx_s)
    await mark_done(point_id)
    points = await get_points(route_id)
    if idx >= len(points) - 1:
        await cb.message.edit_text(
            "🏁 <b>Маршрут завершён</b>\n\n" + summary_text(points),
            parse_mode="HTML",
            reply_markup=summary_kb(route_id),
        )
    else:
        nxt = points[idx + 1]
        await cb.message.edit_text(point_text(nxt, idx + 1, len(points)), parse_mode="HTML", reply_markup=point_kb(nxt, route_id, idx + 1, len(points)))
    await cb.answer("Точка выполнена")


@dp.callback_query(F.data.startswith("list:"))
async def list_points(cb: CallbackQuery):
    _, route_s, page_s = cb.data.split(":")
    route_id, page = int(route_s), int(page_s)
    points = await get_points(route_id)
    per_page = 6
    max_page = max(0, (len(points) - 1) // per_page)
    page = max(0, min(page, max_page))
    start = page * per_page
    chunk = points[start:start + per_page]
    end = start + len(chunk)
    lines = [f"📋 <b>Маршрут · {len(points)} точек</b>   <i>{start+1}–{end}</i>\n"]
    for i, p in enumerate(chunk, start=start):
        mark = "✅" if p["done"] else icon(p["lab_type"])
        note = " 📝" if p["note"] else ""
        lines.append(f"<b>{i+1}.</b> {mark}{note} {p['nav_address']}")
    lines.append("\nНажми номер точки, чтобы открыть её.")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=list_keyboard(route_id, page, start, chunk, len(points)),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("note:"))
async def note_begin(cb: CallbackQuery, state: FSMContext):
    _, point_s, route_s, idx_s = cb.data.split(":")
    await state.set_state(NoteState.waiting)
    await state.update_data(point_id=int(point_s), route_id=int(route_s), idx=int(idx_s))
    await cb.message.answer("📝 Напиши заметку для этой точки: расходники, документы, что забрать или передать.")
    await cb.answer()


@dp.message(NoteState.waiting)
async def note_save(message: Message, state: FSMContext):
    data = await state.get_data()
    await set_note(data["point_id"], message.text.strip())
    point = await get_point(data["point_id"])
    points = await get_points(data["route_id"])
    await state.clear()
    await message.answer(
        "✅ Заметка сохранена.\n\n" + point_text(point, data["idx"], len(points)),
        parse_mode="HTML",
        reply_markup=point_kb(point, data["route_id"], data["idx"], len(points)),
    )


@dp.callback_query(F.data.startswith("add:"))
async def add_begin(cb: CallbackQuery, state: FSMContext):
    route_id = int(cb.data.split(":")[1])
    await state.set_state(AddState.waiting)
    await state.update_data(route_id=route_id)
    await cb.message.answer("➕ Пришли дополнительный адрес текстом. Он будет добавлен в конец маршрута.")
    await cb.answer()


@dp.message(AddState.waiting)
async def add_save(message: Message, state: FSMContext):
    data = await state.get_data()
    address = normalize_address(message.text.strip())
    point_id = await add_manual_point(data["route_id"], address)
    await state.clear()
    points = await get_points(data["route_id"])
    idx = next(i for i, p in enumerate(points) if p["id"] == point_id)
    point = points[idx]
    await message.answer(
        "✅ Дополнительная точка добавлена.\n\n" + point_text(point, idx, len(points)),
        parse_mode="HTML",
        reply_markup=point_kb(point, data["route_id"], idx, len(points)),
    )


async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    await init_db()
    await dp.start_polling(Bot(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
