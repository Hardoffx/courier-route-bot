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
    m = re.search(r"(?:ул\.?|улица|б-р|бульвар|ш\.?|шоссе|пер\.?|переулок|пр-д|проезд)\s+([а-яa-z0-9-]+)", a)
    return "OTHER:" + (m.group(1) if m else a[:18])


def _estimate_schedule(points: list[dict], now_minute: int) -> dict[int, int]:
    minute = now_minute
    previous_cluster = None
    arrivals: dict[int, int] = {}
    for p in points:
        c = _cluster(p.get("nav_address", ""))
        if previous_cluster is None:
            travel = 8
        elif c == previous_cluster:
            travel = 10
        else:
            travel = 22
        minute += travel
        arrivals[p["id"]] = minute
        minute += 7
        previous_cluster = c
    return arrivals


def optimize_remaining_points(points: list[dict], now: datetime | None = None) -> OptimizationResult:
    """Offline fallback: windows + coarse zones, no network required."""
    if now is None:
        now = datetime.now(MOSCOW)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MOSCOW)
    local = now.astimezone(MOSCOW)
    now_minute = local.hour * 60 + local.minute

    done = [p for p in points if p.get("done")]
    remaining = [p for p in points if not p.get("done")]
    original_ids = [p["id"] for p in points]
    if len(remaining) < 2:
        return OptimizationResult(original_ids, 0, [], 0, ["Перестройка не требуется."])

    arrivals = _estimate_schedule(remaining, now_minute)
    urgent = []
    for p in remaining:
        end = _minutes(p.get("window_end"))
        if end is not None and end - arrivals[p["id"]] < 25:
            urgent.append(p)
    if not urgent:
        return OptimizationResult(original_ids, 0, [], 0, ["Все временные окна укладываются в текущий порядок."])

    original_index = {p["id"]: i for i, p in enumerate(remaining)}
    clusters: dict[str, list[dict]] = {}
    cluster_first: dict[str, int] = {}
    for i, p in enumerate(remaining):
        c = _cluster(p.get("nav_address", ""))
        clusters.setdefault(c, []).append(p)
        cluster_first.setdefault(c, i)
    urgent_clusters: dict[str, int] = {}
    for p in urgent:
        c = _cluster(p.get("nav_address", ""))
        end = _minutes(p.get("window_end")) or 10000
        urgent_clusters[c] = min(urgent_clusters.get(c, 10000), end)
    cluster_order = sorted(clusters, key=lambda c: (0 if c in urgent_clusters else 1, urgent_clusters.get(c, 10000), cluster_first[c]))

    reordered = []
    for c in cluster_order:
        block = clusters[c]
        if c in urgent_clusters:
            block = sorted(block, key=lambda p: (_minutes(p.get("window_end")) if _minutes(p.get("window_end")) is not None else 10000, original_index[p["id"]]))
        reordered.extend(block)

    ordered_ids = [p["id"] for p in done] + [p["id"] for p in reordered]
    moved = sum(1 for i, pid in enumerate(ordered_ids) if i < len(original_ids) and pid != original_ids[i])
    before = [_cluster(p.get("nav_address", "")) for p in remaining]
    after = [_cluster(p.get("nav_address", "")) for p in reordered]
    changes = sum(1 for a, b in zip(before, after) if a != b)
    explanation = ["Использован резервный режим по районам: дорожная матрица недоступна."]
    explanation.append("Срочные районы подняты блоками, остальные оставлены близко к исходному порядку.")
    return OptimizationResult(ordered_ids, moved, [p["id"] for p in urgent], changes, explanation)


def _simulate_order(
    order: list[int],
    points: list[dict],
    matrix: list[list[float | None]],
    now_minute: float,
) -> tuple[float, float, list[int], float]:
    """Return drive minutes, max lateness, risky indexes and waiting minutes."""
    t = now_minute
    drive = 0.0
    wait_total = 0.0
    late_max = 0.0
    risky: list[int] = []
    current = 0  # virtual start is original first remaining point
    for step, idx in enumerate(order):
        travel = 0.0 if step == 0 and idx == 0 else matrix[current][idx]
        if travel is None:
            travel = 35.0
        drive += travel
        t += travel
        start = _minutes(points[idx].get("window_start"))
        end = _minutes(points[idx].get("window_end"))
        if start is not None and t < start:
            wait = float(start) - t
            wait_total += wait
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


def _beam_optimize(points: list[dict], matrix: list[list[float | None]], now_minute: float) -> list[int]:
    n = len(points)
    if n <= 1:
        return list(range(n))

    states = [(0.0, now_minute, 0.0, -1, tuple(), frozenset(range(n)))]
    for step in range(n):
        expanded = []
        for score, t, drive, last, path, remaining in states:
            for idx in remaining:
                if last == -1:
                    travel = 0.0 if idx == 0 else (matrix[0][idx] if matrix[0][idx] is not None else 35.0)
                else:
                    travel = matrix[last][idx] if matrix[last][idx] is not None else 35.0
                arrival = t + travel
                start = _minutes(points[idx].get("window_start"))
                end = _minutes(points[idx].get("window_end"))
                wait = max(0.0, float(start) - arrival) if start is not None else 0.0
                service_start = arrival + wait
                lateness = max(0.0, service_start - float(end)) if end is not None else 0.0
                slack = float(end) - service_start if end is not None else 999.0

                penalty = lateness * 3000.0
                if slack < 25:
                    penalty += (25.0 - slack) * 18.0
                penalty += travel
                penalty += abs(idx - step) * 3.5
                if path and abs(idx - path[-1]) > 5:
                    penalty += 7.0

                new_t = service_start + SERVICE_MINUTES
                expanded.append((score + penalty, new_t, drive + travel, idx, path + (idx,), remaining - {idx}))
        expanded.sort(key=lambda s: s[0])
        states = expanded[:BEAM_WIDTH]
    return list(states[0][4]) if states else list(range(n))


async def optimize_remaining_points_live(points: list[dict], now: datetime | None = None) -> OptimizationResult:
    """Use real road travel times when available, with safe offline fallback."""
    if now is None:
        now = datetime.now(MOSCOW)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MOSCOW)
    local = now.astimezone(MOSCOW)
    now_minute = local.hour * 60 + local.minute + local.second / 60.0

    done = [p for p in points if p.get("done")]
    remaining = [p for p in points if not p.get("done")]
    original_ids = [p["id"] for p in points]
    if len(remaining) < 2:
        return OptimizationResult(original_ids, 0, [], 0, ["Перестройка не требуется."], mode="road")

    matrix, geocoded = await road_matrix_for_points(remaining)
    if matrix is None:
        result = optimize_remaining_points(points, now)
        result.geocoded = geocoded
        return result

    original_order = list(range(len(remaining)))
    candidate = _beam_optimize(remaining, matrix, now_minute)
    original_drive, original_late, original_risky, original_wait = _simulate_order(original_order, remaining, matrix, now_minute)
    new_drive, new_late, new_risky, new_wait = _simulate_order(candidate, remaining, matrix, now_minute)

    improves_deadline = new_late + 0.01 < original_late or len(new_risky) < len(original_risky)
    saves_drive = original_drive - new_drive >= 8.0
    if candidate != original_order and not (improves_deadline or saves_drive):
        candidate = original_order
        new_drive, new_late, new_risky, new_wait = original_drive, original_late, original_risky, original_wait

    reordered = [remaining[i] for i in candidate]
    ordered_ids = [p["id"] for p in done] + [p["id"] for p in reordered]
    moved = sum(1 for i, pid in enumerate(ordered_ids) if i < len(original_ids) and pid != original_ids[i])

    before_clusters = [_cluster(p.get("nav_address", "")) for p in remaining]
    after_clusters = [_cluster(p.get("nav_address", "")) for p in reordered]
    cluster_changes = sum(1 for a, b in zip(before_clusters, after_clusters) if a != b)

    explanation = [f"Учтено реальное автомобильное время между {len(remaining)} оставшимися точками."]
    if moved:
        saved = max(0, round(original_drive - new_drive))
        if saved:
            explanation.append(f"Оценочно экономится около {saved} мин. дороги.")
        if len(new_risky) < len(original_risky):
            explanation.append(f"Точек с риском по времени стало меньше: {len(original_risky)} → {len(new_risky)}.")
        explanation.append("Штраф за отклонение от исходного порядка не даёт алгоритму перестраивать маршрут без необходимости.")
    else:
        explanation.append("Исходный порядок уже достаточно хороший: перестановка не даёт существенной пользы.")

    service_minutes = round(SERVICE_MINUTES * len(remaining))
    drive_minutes = round(new_drive)
    wait_minutes = round(new_wait)
    total_minutes = round(new_drive + new_wait + SERVICE_MINUTES * len(remaining))

    return OptimizationResult(
        ordered_ids=ordered_ids,
        moved=moved,
        urgent_ids=[remaining[i]["id"] for i in new_risky],
        cluster_changes=cluster_changes,
        explanation=explanation,
        mode="road",
        geocoded=geocoded,
        estimated_drive_minutes=drive_minutes,
        estimated_service_minutes=service_minutes,
        estimated_wait_minutes=wait_minutes,
        estimated_total_minutes=total_minutes,
    )
