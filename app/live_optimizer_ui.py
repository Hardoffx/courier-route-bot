from __future__ import annotations

from aiogram.types import CallbackQuery

from app.optimizer import optimize_remaining_points_live


def apply(bot_module) -> None:
    """Replace the v1 optimize callback with road-matrix optimization."""

    async def optimize_route_live(cb: CallbackQuery):
        route_id = int(cb.data.split(":")[1])
        points = await bot_module.get_points(route_id)
        if not points:
            await cb.answer("Маршрут пуст", show_alert=True)
            return

        # Answer immediately so Telegram does not show a spinning button while
        # first-time geocoding / road-matrix calculation is running.
        await cb.answer("⚡ Считаю весь маршрут и время в дороге…")

        try:
            result = await optimize_remaining_points_live(points)
        except Exception:
            # The optimizer itself has a fallback, but keep UI safe even if an
            # unexpected provider/network error escapes.
            from app.optimizer import optimize_remaining_points
            result = optimize_remaining_points(points)

        mode_line = (
            "🚗 Использована дорожная матрица между адресами."
            if result.mode == "road"
            else "🧭 Дорожный сервис недоступен — использован резервный расчёт по районам."
        )

        if result.moved == 0:
            extra = [
                "⚡ <b>Проверил весь оставшийся маршрут</b>",
                mode_line,
                "Переставлять точки сейчас невыгодно: исходный порядок уже хороший с учётом времени ЛПУ.",
            ]
            if result.estimated_drive_minutes is not None:
                extra.append(f"Оценка дороги по оставшимся точкам: <b>≈ {result.estimated_drive_minutes} мин.</b>")
            updated = await bot_module.get_points(route_id)
            await cb.message.edit_text(
                bot_module.summary_text(updated, "\n".join(extra)),
                parse_mode="HTML",
                reply_markup=bot_module.summary_kb(route_id),
            )
            return

        for position, point_id in enumerate(result.ordered_ids, 1):
            await bot_module.move_point(route_id, point_id, position)

        updated = await bot_module.get_points(route_id)
        lines = [
            "⚡ <b>Маршрут умно перестроен</b>",
            mode_line,
            f"Изменено позиций: <b>{result.moved}</b> · риск по времени: <b>{len(result.urgent_ids)}</b>",
        ]
        if result.estimated_drive_minutes is not None:
            lines.append(f"Оценка дороги: <b>≈ {result.estimated_drive_minutes} мин.</b>")
        lines.extend(result.explanation[:3])

        await cb.message.edit_text(
            bot_module.summary_text(updated, "\n".join(lines)),
            parse_mode="HTML",
            reply_markup=bot_module.summary_kb(route_id),
        )

    for handler in bot_module.dp.callback_query.handlers:
        callback = getattr(handler, "callback", None)
        if getattr(callback, "__name__", "") == "optimize_route":
            handler.callback = optimize_route_live
            break
