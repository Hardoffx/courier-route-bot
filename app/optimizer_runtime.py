from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.optimizer import (
    OptimizationResult,
    SERVICE_MINUTES,
    _cluster,
    _simulate_order,
    optimize_remaining_points_live as _core_optimize,
)
from app.routing import road_matrix_for_points

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
    """Use real time on a live route and remembered/learned start for a night replay."""
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
    night_recheck = current_minute < 6 * 60 and not has_done

    if not night_recheck and (current_minute <= latest_end + 30 or has_done):
        return current, None

    start_minute = points[0].get("_route_start_minute") if points else None
    source = points[0].get("_route_start_source") if points else None
    try:
        start_minute = int(start_minute)
    except (TypeError, ValueError):
        start_minute = 11 * 60 + 30
        source = "fallback"

    start_minute = max(0, min(start_minute, 23 * 60 + 59))
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


def _changed_positions(order: list[int]) -> int:
    return sum(1 for pos, idx in enumerate(order) if pos != idx)


async def _weekday_optimize(points: list[dict], now: datetime) -> OptimizationResult:
    """Weekdays: preserve the courier-proven order and only remove real opening-time waits.

    The old global beam search was allowed to chase road-time savings and could move
    20 of 24 stops, even putting a remote point first. On weekdays that is the wrong
    objective: the existing order is trusted. We test only one local relocation at a
    time, never replace the first stop, and accept it only when it materially reduces
    waiting for a clinic to start issuing samples without worsening closing risk.
    """
    done = [p for p in points if p.get("done")]
    remaining = [p for p in points if not p.get("done")]
    original_ids = [p["id"] for p in points]
    if len(remaining) < 2:
        return OptimizationResult(original_ids, 0, [], 0, ["Будний режим: перестройка не требуется."], mode="road")

    matrix, geocoded = await road_matrix_for_points(remaining)
    if matrix is None:
        return OptimizationResult(
            original_ids, 0, [], 0,
            ["Будний режим: исходный порядок сохранён — дорожной матрицы недостаточно для безопасной перестановки."],
            mode="fallback", geocoded=geocoded,
        )

    start_minute = now.hour * 60 + now.minute + now.second / 60.0
    original = list(range(len(remaining)))
    orig_drive, orig_late, orig_risky, orig_wait = _simulate_order(original, remaining, matrix, start_minute)

    best = original
    best_metrics = (orig_drive, orig_late, orig_risky, orig_wait)
    best_score = orig_wait * 24.0 + orig_drive + len(orig_risky) * 400.0

    # One local relocation is deliberate. It fixes cases such as Mitinskaya 48
    # opening later than the preceding stops without rebuilding the whole route.
    # Index 0 is pinned: optimization must never replace the known first stop.
    n = len(remaining)
    for src in range(1, n):
        for dst in range(1, n):
            if src == dst or abs(src - dst) > 6:
                continue
            candidate = original.copy()
            item = candidate.pop(src)
            candidate.insert(dst, item)
            drive, late, risky, wait = _simulate_order(candidate, remaining, matrix, start_minute)
            changed = _changed_positions(candidate)

            # Weekday closing times are a safety rail, not the main sorting key.
            # Never trade a new closing problem for a shorter opening-time wait.
            if late > orig_late + 0.01 or len(risky) > len(orig_risky):
                continue
            if wait > orig_wait - 3.0:
                continue

            score = wait * 24.0 + drive + len(risky) * 400.0 + changed * 18.0
            if score + 0.01 < best_score:
                best = candidate
                best_metrics = (drive, late, risky, wait)
                best_score = score

    new_drive, new_late, new_risky, new_wait = best_metrics
    reordered = [remaining[i] for i in best]
    ordered_ids = [p["id"] for p in done] + [p["id"] for p in reordered]
    moved = sum(1 for i, pid in enumerate(ordered_ids) if i < len(original_ids) and pid != original_ids[i])

    before_clusters = [_cluster(p.get("nav_address", "")) for p in remaining]
    after_clusters = [_cluster(p.get("nav_address", "")) for p in reordered]
    cluster_changes = sum(1 for a, b in zip(before_clusters, after_clusters) if a != b)

    explanation = ["Будний режим: сохраняю рабочий порядок и исправляю только слишком ранний приезд к началу выдачи."]
    if best != original:
        saved_wait = max(0, round(orig_wait - new_wait))
        explanation.append(f"Сделана одна локальная перестановка; ожидание открытия сокращено примерно на {saved_wait} мин.")
    elif orig_wait >= 3:
        explanation.append("Безопасной локальной перестановки не найдено: исходный порядок сохранён.")
    else:
        explanation.append("Раннего приезда, требующего перестановки, не прогнозируется.")

    service = round(SERVICE_MINUTES * len(remaining))
    drive = round(new_drive)
    wait = round(new_wait)
    total = round(new_drive + new_wait + SERVICE_MINUTES * len(remaining))
    return OptimizationResult(
        ordered_ids=ordered_ids,
        moved=moved,
        urgent_ids=[remaining[i]["id"] for i in new_risky],
        cluster_changes=cluster_changes,
        explanation=explanation,
        mode="road",
        geocoded=geocoded,
        estimated_drive_minutes=drive,
        estimated_service_minutes=service,
        estimated_wait_minutes=wait,
        estimated_total_minutes=total,
    )


async def optimize_remaining_points_live(points: list[dict], now: datetime | None = None):
    planned_now, source = planning_now(points, now)
    profile = str(points[0].get("_route_profile") or "weekday") if points else "weekday"

    if profile == "weekday":
        result = await _weekday_optimize(points, planned_now)
    else:
        # Weekends keep the deadline-oriented global optimizer: early closing
        # times are the real operational risk there.
        result = await _core_optimize(points, planned_now)
        result.explanation.insert(0, "Выходной режим: приоритет — успеть к раннему закрытию точек.")

    if source:
        label = planned_now.strftime("%H:%M")
        if source == "actual":
            text = f"Проверка рассчитана от фактически запомненного старта маршрута — {label}."
        elif source == "learned":
            text = f"Проверка рассчитана от типичного старта для этого профиля — {label}; RoutePilot уточняет его по реальным маршрутам."
        else:
            text = f"Проверка рассчитана от временной оценки старта — {label}."
        result.explanation.insert(0, text)
    return result
