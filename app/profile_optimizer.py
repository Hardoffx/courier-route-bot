from __future__ import annotations

from datetime import datetime

from app.optimizer import OptimizationResult, SERVICE_MINUTES, _minutes, _cluster, _travel
from app.routing import road_matrix_for_points

BEAM_WIDTH = 220


def _profile(points: list[dict]) -> str:
    value = str(points[0].get("_route_profile") or "weekday") if points else "weekday"
    return "weekend" if value == "weekend" else "weekday"


def _simulate(order, points, matrix, now_minute, profile):
    t = float(now_minute)
    drive = 0.0
    wait_total = 0.0
    late_max = 0.0
    risky: list[int] = []
    current = 0
    risk_margin = 35 if profile == "weekend" else 15

    for step, idx in enumerate(order):
        travel = 0.0 if step == 0 and idx == 0 else _travel(matrix, points, current, idx)
        drive += travel
        t += travel
        start = _minutes(points[idx].get("window_start"))
        end = _minutes(points[idx].get("window_end"))

        if start is not None and t < start:
            wait = float(start) - t
            wait_total += wait
            t = float(start)

        if end is not None:
            slack = float(end) - t
            if slack < risk_margin:
                risky.append(idx)
            if t > end:
                late_max = max(late_max, t - float(end))

        t += SERVICE_MINUTES
        current = idx

    return drive, late_max, risky, wait_total


def _beam(points, matrix, now_minute, profile):
    n = len(points)
    if n <= 1:
        return list(range(n))

    states = [(0.0, float(now_minute), -1, tuple(), frozenset(range(n)))]
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

                # Weekdays: avoid arriving before material is ready. Closing time
                # is only a safety rail, because a normal weekday route has ample
                # time to finish. Weekends: deadlines dominate; points that close
                # early must be brought forward even if that costs some distance.
                if profile == "weekday":
                    penalty = travel
                    penalty += wait * 90.0
                    penalty += lateness * 4000.0
                    if slack < 15:
                        penalty += (15.0 - slack) * 25.0
                    penalty += abs(idx - step) * 2.8
                else:
                    penalty = travel
                    penalty += wait * 3.0
                    penalty += lateness * 7000.0
                    if slack < 40:
                        penalty += (40.0 - slack) * 80.0
                    penalty += abs(idx - step) * 1.6

                # Discourage gratuitous jumps across the sheet when timing does
                # not justify them; geography/travel still remains in the score.
                if path and abs(idx - path[-1]) > 6:
                    penalty += 6.0 if profile == "weekday" else 3.0

                new_t = service_start + SERVICE_MINUTES
                expanded.append((score + penalty, new_t, idx, path + (idx,), remaining - {idx}))

        expanded.sort(key=lambda s: s[0])
        states = expanded[:BEAM_WIDTH]

    return list(states[0][3]) if states else list(range(n))


async def optimize_remaining_points_live(points: list[dict], now: datetime) -> OptimizationResult:
    profile = _profile(points)
    now_minute = now.hour * 60 + now.minute + now.second / 60.0

    done = [p for p in points if p.get("done")]
    remaining = [p for p in points if not p.get("done")]
    original_ids = [p["id"] for p in points]
    if len(remaining) < 2:
        return OptimizationResult(original_ids, 0, [], 0, ["Перестройка не требуется."], mode="road")

    matrix, geocoded = await road_matrix_for_points(remaining)
    if matrix is None:
        # Keep the route stable when road data is unavailable. Reordering on
        # coarse estimates is worse than preserving the learned/manual order.
        return OptimizationResult(
            original_ids, 0, [], 0,
            ["Дорожная матрица недоступна; порядок оставлен без изменений."],
            mode="fallback", geocoded=geocoded,
        )

    original = list(range(len(remaining)))
    candidate = _beam(remaining, matrix, now_minute, profile)

    o_drive, o_late, o_risky, o_wait = _simulate(original, remaining, matrix, now_minute, profile)
    n_drive, n_late, n_risky, n_wait = _simulate(candidate, remaining, matrix, now_minute, profile)

    if profile == "weekday":
        materially_better = (
            n_late + 0.01 < o_late
            or o_wait - n_wait >= 4.0
            or (n_wait <= o_wait + 0.5 and o_drive - n_drive >= 8.0)
        )
    else:
        materially_better = (
            n_late + 0.01 < o_late
            or len(n_risky) < len(o_risky)
            or (n_late <= o_late + 0.01 and len(n_risky) <= len(o_risky) and o_drive - n_drive >= 8.0)
        )

    if candidate != original and not materially_better:
        candidate = original
        n_drive, n_late, n_risky, n_wait = o_drive, o_late, o_risky, o_wait

    reordered = [remaining[i] for i in candidate]
    ordered_ids = [p["id"] for p in done] + [p["id"] for p in reordered]
    moved = sum(1 for i, pid in enumerate(ordered_ids) if i < len(original_ids) and pid != original_ids[i])

    before_clusters = [_cluster(p.get("nav_address", "")) for p in remaining]
    after_clusters = [_cluster(p.get("nav_address", "")) for p in reordered]
    cluster_changes = sum(1 for a, b in zip(before_clusters, after_clusters) if a != b)

    if profile == "weekday":
        explanation = ["Режим буднего дня: главный приоритет — не приехать раньше начала выдачи."]
        if o_wait > n_wait + 0.5:
            explanation.append(f"Ожидание открытия сокращено примерно на {round(o_wait - n_wait)} мин.")
        explanation.append("Время закрытия используется как страховка и не ломает хороший географический порядок без реального риска опоздания.")
    else:
        explanation = ["Режим выходного дня: главный приоритет — успеть до закрытия точек."]
        if len(n_risky) < len(o_risky):
            explanation.append(f"Точек с риском опоздания стало меньше: {len(o_risky)} → {len(n_risky)}.")
        explanation.append("Начало выдачи учитывается, но ранние закрытия имеют более высокий приоритет, чем небольшая экономия дороги.")

    coverage = geocoded / max(1, len(remaining))
    if coverage < 1:
        explanation.append(f"Дорожная матрица: {geocoded}/{len(remaining)} адресов; пропуски оценены по району.")

    service_minutes = round(SERVICE_MINUTES * len(remaining))
    drive_minutes = round(n_drive)
    wait_minutes = round(n_wait)
    total_minutes = round(n_drive + n_wait + SERVICE_MINUTES * len(remaining))

    return OptimizationResult(
        ordered_ids=ordered_ids,
        moved=moved,
        urgent_ids=[remaining[i]["id"] for i in n_risky],
        cluster_changes=cluster_changes,
        explanation=explanation,
        mode="road",
        geocoded=geocoded,
        estimated_drive_minutes=drive_minutes,
        estimated_service_minutes=service_minutes,
        estimated_wait_minutes=wait_minutes,
        estimated_total_minutes=total_minutes,
    )
