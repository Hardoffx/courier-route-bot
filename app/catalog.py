from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from app.normalizer import canonical_key, house_signature


@dataclass(frozen=True)
class KnownPoint:
    nav_address: str
    lab_type: str
    facility_code: str | None = None


# Canonical navigation strings verified against the courier's real route sheets.
KNOWN_POINTS: list[KnownPoint] = [
    KnownPoint("г Красногорск, тер. Детский клинический центр к1", "INVITRO"),
    KnownPoint("Москва г, ул Генерала Белобородова 19к1", "INVITRO"),
    KnownPoint("Москва г, ул Дубравная 46", "CMD", "458"),
    KnownPoint("Москва г, пер 3-й Митинский 7", "INVITRO"),
    KnownPoint("Москва г, Митинский 3-й пер 4к1", "CMD", "2939"),
    KnownPoint("Москва г, ул Митинская 10", "INVITRO"),
    KnownPoint("Москва г, ул Митинская 48", "INVITRO"),
    KnownPoint("Москва, ул Митинская 57", "INVITRO"),
    KnownPoint("г Красногорск, п Отрадное, ул Кленовая 3", "INVITRO"),
    KnownPoint("деревня Юрлово, 87, Московская область", "CMD", "5827"),
    KnownPoint("Новое Аристово, ул Солнечная 5", "OTHER"),
    KnownPoint("Москва г, ул Митинская 44", "CMD", "3433"),
    KnownPoint("Москва г, ул Митинская 27", "INVITRO"),
    KnownPoint("Москва г, ул Митинская 17к4", "CMD", "1346"),
    KnownPoint("Москва г, ул Дубравная 41к2", "CMD", "1720"),
    KnownPoint("Москва г, ул Маршала Катукова 6", "CMD", "251"),
    KnownPoint("Москва г, Строгинский б-р 10к3", "CMD", "1513"),
    KnownPoint("Москва г, ул Маршала Катукова 24к5", "CMD", "1370"),
    KnownPoint("Москва г, ш Волоколамское 71к2", "INVITRO"),
    KnownPoint("Москва г, ул Габричевского 10к1", "INVITRO"),
    KnownPoint("Москва г, ул Вишнёвая 13к1 стр. 1", "INVITRO"),
    KnownPoint("Москва г, б-р Химкинский 9", "INVITRO"),
    KnownPoint("Москва г, ул Планерная 5", "INVITRO"),
    KnownPoint("Москва г, б-р Яна Райниса 10", "INVITRO"),
    # Alternate CMD points seen on other route sheets.
    KnownPoint("Москва г, б-р Яна Райниса 31", "CMD", "3885"),
    KnownPoint("Москва г, ул Нелидовская 20к1", "CMD", "1645"),
    KnownPoint("Москва г, ул Планерная 3к1", "CMD", "6935"),
]


def resolve_known_full(address: str) -> KnownPoint | None:
    key = canonical_key(address)
    sig = house_signature(address)
    best: KnownPoint | None = None
    best_score = 0.0

    # Yurlovo is a known courier point whose route sheets can contain an
    # administrative form or even an inaccurate house number. Always resolve
    # the locality to the courier-confirmed house 87.
    if "юрлово" in key:
        return next(point for point in KNOWN_POINTS if "юрлово" in canonical_key(point.nav_address))

    for point in KNOWN_POINTS:
        candidate_key = canonical_key(point.nav_address)
        if key == candidate_key:
            return point
        candidate_sig = house_signature(point.nav_address)
        if sig and candidate_sig and sig != candidate_sig:
            continue
        score = fuzz.ratio(key, candidate_key)
        if score > best_score:
            best_score = score
            best = point

    return best if best_score >= 88 else None


def resolve_known(address: str) -> tuple[str, str] | None:
    point = resolve_known_full(address)
    return (point.nav_address, point.lab_type) if point else None
