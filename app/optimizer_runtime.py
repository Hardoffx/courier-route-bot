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
    """Use the real clock during work and the learned/actual route start for retrospective checks.

    A plain minute-of-day comparison breaks after midnight: 00:30 looks
    'earlier than an 18:00 closing time' and the optimizer then waits ~10 hours
    for clinics to open. Night-time checks of an untouched route are therefore
    retrospective by definition.
    """
    current = now or datetime.now(MOSCOW)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW)
    current = current.astimezone(MOSCOW)

    remaining = [p for p in points if not p.get("done")]
    starts = [_minutes(p.get("window_start")) for p in remaining]
    starts = [value for value in starts if value is not None]
    ends = [_minutes(p.get("window_end")) for p in remaining]
    ends = [value for value in ends if value is not None]
    if not ends:
        return current, None

    current_minute = current.hour * 60 + current.minute
    latest_end = max(ends)
    has_done = any(p.get("done") for p in points)

    # 00:00–05:59 is never a live courier run for this route profile. Without
    # this guard, 00:xx is numerically less than 18:00/20:00 and creates huge
    # fake "waiting for opening" totals.
    night_recheck = current_minute < 6 * 60 and not has_done

    # During a live route use the actual current time. A completed/partly
    # completed route is also live/history-aware and must not be rewound.
    if not night_recheck and (current_minute <= latest_end + 30 or has_done):
        return current, None

    start_minute = points[0].get("_route_start_minute") if points else None
    source = points[0].get("_route_start_source") if points else None
    try:
        start_minute = int(start_minute)
    except (TypeError, ValueError):
        # Seed only until enough real route starts have been learned.
        start_minute = 11 * 60 + 30
        source = "fallback"

    start_minute = max(0, min(start_minute, 23 * 60 + 59))

    # Sanity guard: OCR can occasionally produce a bogus late opening time.
    # If the learned start is already after the earliest normal opening, keep
    # it. We do not force any clinic-specific opening wait here; the core
    # simulator handles valid windows per point.
    if starts:
        earliest_start = min(starts)
        if earliest_start >= 6 * 60 and start_minute < 6 * 60:
            start_minute = earliest_start

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
