from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from app.routing import road_matrix_for_points

MOSCOW = ZoneInfo("Europe/Moscow")
SERVICE_MINUTES = 7.0
BEAM_WIDTH = 180


@dataclass
class OptimizationResult:
    ordered_ids: list[int]
    moved: int
    urgent_ids: list[int]
    cluster_changes: int
    explanation: list[str]
    mode: str = "fallback"
    geocoded: int = 0
    estimated_drive_minutes: int | None = None
    estimated_service_minutes: int | None = None
    estimated_wait_minutes: int | None = None
    estimated_total_minutes: int | None = None


def _minutes(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", value)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _cluster(address: str) -> str:
    a = address.lower().replace("ё", "е")
    rules = [
        ("OUTER_NW", ("красногорск", "отрадное", "юрлово", "аристово", "солнечногор")),
        ("MITINO", ("митинск", "дубравн", "белобород", "новотушин")),
        ("STROGINO", ("строгин", "маршала катуков")),
        ("TUSHINO", ("планерн", "яна райниса", "нелидов", "химкинск", "свобод")),
        ("POKROVSKOE", ("волоколамск", "габричев", "вишнев")),
    ]
    for name, needles in rules:
        if any(x in a for x in needles):
            return name
    return "OTHER"


def _fallback_travel(points: list[dict], a: int, b: int) -> float:
    if a == b:
        return 0.0
    ca = _cluster(points[a].get("nav_address", ""))
    cb = _cluster(points[b].get("nav_address", ""))
    if ca == cb:
        return 7.0
    pair = frozenset((ca, cb))
    adjacent = {
        frozenset(("MITINO", "OUTER_NW")),
        frozenset(("MITINO", "STROGINO")),
        frozenset(("STROGINO", "TUSHINO")),
        frozenset(("STROGINO", "POKROVSKOE")),
        frozenset(("TUSHINO", "POKROVSKOE")),
    }
    medium = {
        frozenset(("MITINO", "TUSHINO")),
        frozenset(("MITINO", "POKROVSKOE")),
        frozenset(("OUTER_NW", "STROGINO")),
    }
    if pair in adjacent:
        return 12.0
    if pair in medium:
        return 16.0
    return 20.0


def _travel(matrix: list[list[float | None]], points: list[dict], a: int, b: int) -> float:
    value = matrix[a][b]
    if value is None:
        return _fallback_travel(points, a, b)
    # Public geocoders occasionally put one house at a wrong location. Do not
    # let one absurd leg destroy a route already proven in daily work.
    fallback = _fallback_travel(points, a, b)
    raw = float(value)
    return min(raw, max(fallback * 2.2, fallback + 12.0))


def _profile(points: list[dict]) -> str:
    value = str(points[0].get("_route_profile") or "") if points else ""
    if value in {"weekday", "weekend"}:
        return value
    return "weekday"


def _addr(point: dict) -> str:
    return str(point.get("nav_address") or "").lower().replace("ё", "е")


def _weekday_manual_baseline(points: list[dict]) -> list[dict]:
    """Keep the courier-proven sheet order and only encode the known outer-NW block.

    The reliable weekday route is the uploaded sheet order. The one permanent
    correction confirmed by the courier is:
      Митинская 57 -> Юрлово 89 -> Новое Аристово/Солнечная 5 -> Кленовая 3.
    Everything else stays where it came from (or where the user saved it).
    """
    ordered = list(points)

    def find(needles: tuple[str, ...]) -> dict | None:
        return next((p for p in ordered if all(n in _addr(p) for n in needles)), None)

    anchor = find(("митинская", "57"))
    yurlovo = find(("юрлово", "89"))
    aristovo = find(("аристово", "солнеч"))
    klen = find(("кленовая", "3"))
    block = [p for p in (yurlovo, aristovo, klen) if p is not None]
    if anchor is None or len(block) < 2:
        return ordered

    for p in block:
        if p in ordered:
            ordered.remove(p)
    anchor_index = ordered.index(anchor) + 1
    for offset, p in enumerate(block):
        ordered.insert(anchor_index + offset, p)
    return ordered


def _simulate_order(order: list[int], points: list[dict], matrix: list[list[float | None]], now_minute: float) -> tuple[float, float, list[int], float]:
    t = now_minute
    drive = 0.0
    wait_total = 0.0
    late_max = 0.0
    risky: list[int] = []
    current = 0
    for step, idx in enumerate(order):
        travel = 0.0 if step == 0 and idx == 0 else _travel(matrix, points, current, idx)
        drive += travel
        t += travel
        start = _minutes(points[idx].get("window_start"))
        end = _minutes(points[idx].get("window_end"))
        if start is not None and t < start:
            wait_total += float(start) - t
            t = float(start)
        if end is not None:
            slack = end - t
            if slack < 25:
                risky.append(idx)
            if t > end:
                late_max = max(late_max, t - end)
        t += SERVICE_MINUTES
        current = idx
    return drive, late_max, risky, wait_total


def _weekday_local_fix(points: list[dict], matrix: list[list[float | None]], now_minute: float) -> list[int]:
    """At most one small move, only to avoid arriving before collection starts."""
    original = list(range(len(points)))
    if len(points) < 3:
        return original

    base_drive, base_late, base_risky, base_wait = _simulate_order(original, points, matrix, now_minute)
    best = original
    best_score = base_wait

    # The first point is fixed. Search only a short local neighbourhood; this
    # prevents distant places such as Новое Аристово becoming point #1.
    for src in range(1, len(points)):
        for dst in range(max(1, src - 3), min(len(points), src + 5)):
            if dst == src:
                continue
            candidate = original.copy()
            item = candidate.pop(src)
            candidate.insert(dst, item)
            drive, late, risky, wait = _simulate_order(candidate, points, matrix, now_minute)

            # Weekday correction must be about opening time, not shaving road.
            wait_gain = base_wait - wait
            if wait_gain < 4.0:
                continue
            if late > base_late + 0.01 or len(risky) > len(base_risky):
                continue
            if drive > base_drive + 8.0:
                continue

            # Strongly prefer the smallest possible change.
            movement = abs(dst - src)
            score = wait + movement * 1.5 + max(0.0, drive - base_drive) * 0.5
            if score + 0.01 < best_score:
                best_score = score
                best = candidate
    return best


def _beam_optimize_weekend(points: list[dict], matrix: list[list[float | None]], now_minute: float) -> list[int]:
    n = len(points)
    states = [(0.0, now_minute, -1, tuple(), frozenset(range(n)))]
    for step in range(n):
        expanded = []
        for score, t, last, path, remaining in states:
            for idx in remaining:
                if last == -1:
                    travel = 0.0 if idx == 0 else _travel(matrix, points, 0, idx)
                else:
                    travel = _travel(matrix, points, last, idx)
                arrival = t + travel
                start = _minutes(points[idx].get("window_start"))
                end = _minutes(points[idx].get("window_end"))
                wait = max(0.0, float(start) - arrival) if start is not None else 0.0
                service_start = arrival + wait
                lateness = max(0.0, service_start - float(end)) if end is not None else 0.0
                slack = float(end) - service_start if end is not None else 999.0

                penalty = lateness * 5000.0
                if slack < 40:
                    penalty += (40.0 - slack) * 30.0
                penalty += travel
                penalty += wait * 0.5
                penalty += abs(idx - step) * 2.0
                if path and abs(idx - path[-1]) > 6:
                    penalty += 5.0
                expanded.append((score + penalty, service_start + SERVICE_MINUTES, idx, path + (idx,), remaining - {idx}))
        expanded.sort(key=lambda s: s[0])
        states = expanded[:BEAM_WIDTH]
    return list(states[0][3]) if states else list(range(n))


def optimize_remaining_points(points: list[dict], now: datetime | None = None) -> OptimizationResult:
    """Safe offline fallback: never globally scramble a weekday route."""
    ids = [p["id"] for p in points]
    return OptimizationResult(ids, 0, [], 0, ["Дорожная матрица недоступна: сохранён проверенный порядок маршрута."])


async def optimize_remaining_points_live(points: list[dict], now: datetime | None = None) -> OptimizationResult:
    if now is None:
        now = datetime.now(MOSCOW)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MOSCOW)
    local = now.astimezone(MOSCOW)
    now_minute = local.hour * 60 + local.minute + local.second / 60.0

    done = [p for p in points if p.get("done")]
    remaining = [p for p in points if not p.get("done")]
    original_ids = [p["id"] for p in points]
    profile = _profile(points)
    if len(remaining) < 2:
        return OptimizationResult(original_ids, 0, [], 0, ["Перестройка не требуется."], mode="road")

    # Weekdays begin from the courier-confirmed baseline before any calculation.
    if profile == "weekday":
        remaining = _weekday_manual_baseline(remaining)

    matrix, geocoded = await road_matrix_for_points(remaining)
    if matrix is None:
        ordered_ids = [p["id"] for p in done] + [p["id"] for p in remaining]
        moved = sum(1 for a, b in zip(original_ids, ordered_ids) if a != b)
        text = "Будни: сохранён проверенный порядок; применён только постоянный участок Митинская 57 → Юрлово → Солнечная 5 → Кленовая 3." if profile == "weekday" else "Выходные: дорожная матрица недоступна, порядок оставлен без рискованной перестройки."
        return OptimizationResult(ordered_ids, moved, [], 0, [text], mode="fallback", geocoded=geocoded)

    original_order = list(range(len(remaining)))
    if profile == "weekday":
        candidate = _weekday_local_fix(remaining, matrix, now_minute)
    else:
        candidate = _beam_optimize_weekend(remaining, matrix, now_minute)

    base_drive, base_late, base_risky, base_wait = _simulate_order(original_order, remaining, matrix, now_minute)
    new_drive, new_late, new_risky, new_wait = _simulate_order(candidate, remaining, matrix, now_minute)

    if profile == "weekend":
        improves_deadline = new_late + 0.01 < base_late or len(new_risky) < len(base_risky)
        if candidate != original_order and not improves_deadline and base_drive - new_drive < 10.0:
            candidate = original_order
            new_drive, new_late, new_risky, new_wait = base_drive, base_late, base_risky, base_wait

    reordered = [remaining[i] for i in candidate]
    ordered_ids = [p["id"] for p in done] + [p["id"] for p in reordered]
    moved = sum(1 for a, b in zip(original_ids, ordered_ids) if a != b)

    if profile == "weekday":
        explanation = ["Будни: проверенный маршрут сохранён; глобальная перестройка отключена."]
        explanation.append("Постоянный участок: Митинская 57 → Юрлово 89 → Солнечная 5 → Кленовая 3.")
        if candidate != original_order:
            explanation.append("Сделана одна локальная перестановка, чтобы не приехать раньше начала выдачи.")
        else:
            explanation.append("Дополнительных перестановок по времени не требуется.")
    else:
        explanation = ["Выходные: приоритет — успеть к ранним закрытиям."]
        if len(new_risky) < len(base_risky):
            explanation.append(f"Точек с риском опоздания стало меньше: {len(base_risky)} → {len(new_risky)}.")
        elif candidate == original_order:
            explanation.append("Исходный порядок безопаснее или не хуже предложенных перестановок.")

    service_minutes = round(SERVICE_MINUTES * len(remaining))
    drive_minutes = round(new_drive)
    wait_minutes = round(new_wait)
    total_minutes = round(new_drive + new_wait + SERVICE_MINUTES * len(remaining))

    return OptimizationResult(
        ordered_ids=ordered_ids,
        moved=moved,
        urgent_ids=[remaining[i]["id"] for i in new_risky],
        cluster_changes=0,
        explanation=explanation,
        mode="road",
        geocoded=geocoded,
        estimated_drive_minutes=drive_minutes,
        estimated_service_minutes=service_minutes,
        estimated_wait_minutes=wait_minutes,
        estimated_total_minutes=total_minutes,
    )
