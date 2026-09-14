from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass
class OptimizationResult:
    ordered_ids: list[int]
    moved: int
    urgent_ids: list[int]
    cluster_changes: int
    explanation: list[str]


def _minutes(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", value)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _cluster(address: str) -> str:
    """Coarse geography for the courier's north-west Moscow corridor.

    The optimizer intentionally works with large zones instead of individual
    streets. That prevents pulling one urgent point into the route and then
    forcing the courier to leave the district and return to it later.
    Unknown addresses are grouped by their first meaningful street token.
    """
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
    """Estimate arrival minutes using conservative intra/inter-zone travel.

    This is not navigation ETA. It is used only to identify time-window risk.
    """
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
        minute += 7  # handoff / parking / sample pickup reserve
        previous_cluster = c
    return arrivals


def optimize_remaining_points(points: list[dict], now: datetime | None = None) -> OptimizationResult:
    """Reorder only unfinished points while keeping the route recognisable.

    Strategy:
    1. Keep completed points fixed.
    2. Detect deadlines that are at risk on the current order.
    3. Promote the *whole geographic cluster* containing an at-risk point,
       instead of pulling one address out and causing a later return.
    4. Preserve original order inside each cluster as much as possible.
    5. Do not change anything when deadlines do not justify it.
    """
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
    urgent: list[dict] = []
    for p in remaining:
        end = _minutes(p.get("window_end"))
        if end is None:
            continue
        slack = end - arrivals[p["id"]]
        # Less than 25 minutes reserve at estimated arrival = risk.
        if slack < 25:
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
        end = _minutes(p.get("window_end")) or 10_000
        urgent_clusters[c] = min(urgent_clusters.get(c, 10_000), end)

    # Urgent zones first by earliest closing time. Other zones keep the same
    # first-seen order, so the result stays close to the paper route.
    cluster_order = sorted(
        clusters,
        key=lambda c: (
            0 if c in urgent_clusters else 1,
            urgent_clusters.get(c, 10_000),
            cluster_first[c],
        ),
    )

    reordered_remaining: list[dict] = []
    for c in cluster_order:
        block = clusters[c]
        # Within an urgent zone, only small local adjustment: points with a
        # closing time come first, ties keep original order.
        if c in urgent_clusters:
            block = sorted(
                block,
                key=lambda p: (
                    _minutes(p.get("window_end")) if _minutes(p.get("window_end")) is not None else 10_000,
                    original_index[p["id"]],
                ),
            )
        reordered_remaining.extend(block)

    ordered_ids = [p["id"] for p in done] + [p["id"] for p in reordered_remaining]
    moved = sum(1 for i, pid in enumerate(ordered_ids) if i < len(original_ids) and pid != original_ids[i])

    before_clusters = [_cluster(p.get("nav_address", "")) for p in remaining]
    after_clusters = [_cluster(p.get("nav_address", "")) for p in reordered_remaining]
    cluster_changes = sum(1 for a, b in zip(before_clusters, after_clusters) if a != b)

    explanation = []
    for c in cluster_order:
        if c in urgent_clusters:
            hh, mm = divmod(urgent_clusters[c], 60)
            count = len(clusters[c])
            explanation.append(f"Поднят целиком район/кластер ({count} точ.) из-за закрытия до {hh:02d}:{mm:02d}.")
    explanation.append("Остальные районы сохранены максимально близко к исходному порядку.")

    return OptimizationResult(
        ordered_ids=ordered_ids,
        moved=moved,
        urgent_ids=[p["id"] for p in urgent],
        cluster_changes=cluster_changes,
        explanation=explanation,
    )
