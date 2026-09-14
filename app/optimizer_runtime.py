from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.optimizer import optimize_remaining_points_live as _core_optimize

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_ROUTE_START_HOUR = 12


def _minutes(value: str | None) -> int | None:
    if not value or ":" not in value:
        return None
    try:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute[:2])
    except (TypeError, ValueError):
        return None


def planning_now(points: list[dict], now: datetime | None = None) -> tuple[datetime, bool]:
    """Choose a sane planning clock for a route.

    During the working day the optimizer uses the real current Moscow time.
    When a completed/old route is tested late at night, all LPU windows are
    already closed. Feeding 23:xx into the deadline optimizer makes every stop
    late and can produce a nonsensical full-route reshuffle. In that case plan
    the route from the courier's normal 12:00 start instead.
    """
    current = now or datetime.now(MOSCOW)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW)
    current = current.astimezone(MOSCOW)

    remaining = [p for p in points if not p.get("done")]
    ends = [_minutes(p.get("window_end")) for p in remaining]
    ends = [value for value in ends if value is not None]
    if not ends:
        return current, False

    current_minute = current.hour * 60 + current.minute
    latest_end = max(ends)
    if current_minute > latest_end + 30:
        planned = current.replace(
            hour=DEFAULT_ROUTE_START_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        return planned, True
    return current, False


async def optimize_remaining_points_live(points: list[dict], now: datetime | None = None):
    planned_now, reset_to_route_start = planning_now(points, now)
    result = await _core_optimize(points, planned_now)
    if reset_to_route_start:
        result.explanation.insert(
            0,
            "Расчёт выполнен от 12:00: текущее время уже позже всех окон ЛПУ, поэтому ночное время не использовалось для перестановки.",
        )
    return result
