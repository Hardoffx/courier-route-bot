from datetime import datetime
from zoneinfo import ZoneInfo

from app.optimizer import optimize_remaining_points

MOSCOW = ZoneInfo("Europe/Moscow")


def p(pid, address, end=None, done=0):
    return {
        "id": pid,
        "nav_address": address,
        "window_end": end,
        "window_start": None,
        "done": done,
    }


def test_keeps_route_when_windows_are_safe():
    points = [
        p(1, "Москва г, ул Митинская 10", "18:00"),
        p(2, "Москва г, ул Митинская 48", "18:00"),
        p(3, "Москва г, ул Планерная 5", "18:00"),
    ]
    result = optimize_remaining_points(points, datetime(2026, 9, 14, 10, 0, tzinfo=MOSCOW))
    assert result.ordered_ids == [1, 2, 3]
    assert result.moved == 0


def test_promotes_whole_cluster_for_urgent_point():
    points = [
        p(1, "Москва г, ул Митинская 10", "18:00"),
        p(2, "Москва г, ул Митинская 48", "18:00"),
        p(3, "Москва г, ул Планерная 5", "10:45"),
        p(4, "Москва г, б-р Яна Райниса 10", "18:00"),
        p(5, "Москва г, ул Дубравная 46", "18:00"),
    ]
    result = optimize_remaining_points(points, datetime(2026, 9, 14, 10, 0, tzinfo=MOSCOW))
    assert result.ordered_ids[:2] == [3, 4]
    assert result.moved > 0
    assert 3 in result.urgent_ids


def test_completed_points_stay_in_front():
    points = [
        p(1, "Москва г, ул Митинская 10", None, done=1),
        p(2, "Москва г, ул Митинская 48", "18:00"),
        p(3, "Москва г, ул Планерная 5", "10:30"),
        p(4, "Москва г, б-р Яна Райниса 10", "18:00"),
    ]
    result = optimize_remaining_points(points, datetime(2026, 9, 14, 10, 0, tzinfo=MOSCOW))
    assert result.ordered_ids[0] == 1
