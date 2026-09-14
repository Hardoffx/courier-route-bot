from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.optimizer import optimize_remaining_points_live as _core_optimize

MOSCOW = ZoneInfo("Europe/Moscow")


def _minutes(value: str | None) -> int | None:
    if not value or ":" not in value:
        return None
    try:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute[:2])
    except (TypeError, ValueError):
        return None


def planning_now(points: list[dict], now: datetime | None = None) -> tuple[datetime, str | None]:
    """Use real current time during work and route-specific start for retrospective planning."""
    current = now or datetime.now(MOSCOW)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW)
    current = current.astimezone(MOSCOW)

    remaining = [p for p in points if not p.get("done")]
    ends = [_minutes(p.get("window_end")) for p in remaining]
    ends = [value for value in ends if value is not None]
    if not ends:
        return current, None

    current_minute = current.hour * 60 + current.minute
    latest_end = max(ends)
    # During a live route always use the real clock. Only an evening/night
    # re-check of an untouched route needs its original/learned start time.
    if current_minute <= latest_end + 30 or any(p.get("done") for p in points):
        return current, None

    start_minute = points[0].get("_route_start_minute") if points else None
    source = points[0].get("_route_start_source") if points else None
    try:
        start_minute = int(start_minute)
    except (TypeError, ValueError):
        start_minute = 11 * 60 + 30
        source = "fallback"

    start_minute = max(0, min(start_minute, 23 * 60 + 59))
    planned = current.replace(
        hour=start_minute // 60,
        minute=start_minute % 60,
        second=0,
        microsecond=0,
    )
    return planned, str(source or "learned")


async def optimize_remaining_points_live(points: list[dict], now: datetime | None = None):
    planned_now, source = planning_now(points, now)
    result = await _core_optimize(points, planned_now)
    if source:
        label = planned_now.strftime("%H:%M")
        if source == "actual":
            text = f"Вечерняя проверка рассчитана от фактически запомненного старта маршрута — {label}."
        elif source == "learned":
            text = f"Вечерняя проверка рассчитана от типичного старта для этого профиля — {label}; RoutePilot уточняет его по реальным маршрутам."
        else:
            text = f"Вечерняя проверка рассчитана от временной оценки старта — {label}."
        result.explanation.insert(0, text)
    return result
