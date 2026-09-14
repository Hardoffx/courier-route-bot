from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

import aiosqlite

MOSCOW = ZoneInfo("Europe/Moscow")
SERVICE_MINUTES = 7


def install(db_module, bot_module) -> None:
    original_init_db = bot_module.init_db
    original_create_route = bot_module.create_route
    original_get_points = bot_module.get_points
    original_mark_done = bot_module.mark_done

    async def ensure_timing_schema() -> None:
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cur = await db.execute("PRAGMA table_info(routes)")
            names = {row[1] for row in await cur.fetchall()}
            if "started_at" not in names:
                await db.execute("ALTER TABLE routes ADD COLUMN started_at TEXT")
            await db.commit()

    async def init_db():
        await original_init_db()
        await ensure_timing_schema()

    async def create_route(chat_id: int, route_date: str, image_path: str | None, rows: list[dict]) -> int:
        await ensure_timing_schema()
        # If the same day's route is OCRed again in the evening, carry over the
        # actual start captured during work instead of treating it as a new day.
        previous_start = None
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cur = await db.execute(
                "SELECT started_at FROM routes WHERE chat_id=? AND route_date=? AND started_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (chat_id, route_date),
            )
            row = await cur.fetchone()
            previous_start = row[0] if row else None

        route_id = await original_create_route(chat_id, route_date, image_path, rows)
        if previous_start:
            async with aiosqlite.connect(db_module.DB_PATH) as db:
                await db.execute("UPDATE routes SET started_at=? WHERE id=?", (previous_start, route_id))
                await db.commit()
        return route_id

    async def _profile_start(chat_id: int, route_date: str) -> int:
        profile = db_module.route_profile(route_date)
        values: list[int] = []
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cur = await db.execute(
                "SELECT route_date,started_at FROM routes WHERE chat_id=? AND started_at IS NOT NULL ORDER BY id DESC LIMIT 30",
                (chat_id,),
            )
            for day, value in await cur.fetchall():
                if db_module.route_profile(day) != profile:
                    continue
                try:
                    dt = datetime.fromisoformat(value)
                    values.append(dt.hour * 60 + dt.minute)
                except Exception:
                    continue
                if len(values) >= 10:
                    break
        if values:
            return int(median(values))
        # Only a last-resort seed until RoutePilot has learned real starts.
        return 11 * 60 if profile == "weekend" else 11 * 60 + 30

    async def get_points(route_id: int):
        points = await original_get_points(route_id)
        if not points:
            return points
        await ensure_timing_schema()
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cur = await db.execute("SELECT chat_id,route_date,started_at FROM routes WHERE id=?", (route_id,))
            route = await cur.fetchone()
        if not route:
            return points
        chat_id, route_date, started_at = route
        if started_at:
            try:
                dt = datetime.fromisoformat(started_at)
                start_minute = dt.hour * 60 + dt.minute
                start_source = "actual"
            except Exception:
                start_minute = await _profile_start(chat_id, route_date)
                start_source = "learned"
        else:
            start_minute = await _profile_start(chat_id, route_date)
            start_source = "learned"
        for point in points:
            point["_route_start_minute"] = start_minute
            point["_route_start_source"] = start_source
        return points

    async def mark_done(point_id: int):
        await ensure_timing_schema()
        now = datetime.now(MOSCOW)
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            cur = await db.execute("SELECT route_id FROM route_points WHERE id=?", (point_id,))
            row = await cur.fetchone()
            route_id = row[0] if row else None
            if route_id is not None:
                cur = await db.execute("SELECT started_at FROM routes WHERE id=?", (route_id,))
                route = await cur.fetchone()
                cur = await db.execute("SELECT COUNT(*) FROM route_points WHERE route_id=? AND done=1", (route_id,))
                done_before = int((await cur.fetchone())[0])
                if route and not route[0] and done_before == 0:
                    # The first point is normally marked done after pickup. Back
                    # up by the standard service reserve to approximate the time
                    # the courier entered/started the first point.
                    started = now - timedelta(minutes=SERVICE_MINUTES)
                    await db.execute("UPDATE routes SET started_at=? WHERE id=?", (started.isoformat(timespec="seconds"), route_id))
                    await db.commit()
        await original_mark_done(point_id)

    bot_module.init_db = init_db
    bot_module.create_route = create_route
    bot_module.get_points = get_points
    bot_module.mark_done = mark_done
