from __future__ import annotations

from rapidfuzz import fuzz

from app.normalizer import canonical_key, house_signature

# Canonical navigation strings verified against the courier's real route sheet.
KNOWN_POINTS: list[tuple[str, str]] = [
    ("г Красногорск, тер. Детский клинический центр к1", "INVITRO"),
    ("Москва г, ул Генерала Белобородова 19к1", "INVITRO"),
    ("Москва г, ул Дубравная 46", "CMD"),
    ("Москва г, пер 3-й Митинский 7", "INVITRO"),
    ("Москва г, Митинский 3-й пер 4к1", "CMD"),
    ("Москва г, ул Митинская 10", "INVITRO"),
    ("Москва г, ул Митинская 48", "INVITRO"),
    ("Москва, ул Митинская 57", "INVITRO"),
    ("г Красногорск, п Отрадное, ул Кленовая 3", "INVITRO"),
    ("Московская обл, Солнечногорский р-н, Юрлово 89", "CMD"),
    ("Новое Аристово, ул Солнечная 5", "OTHER"),
    ("Москва г, ул Митинская 44", "CMD"),
    ("Москва г, ул Митинская 27", "INVITRO"),
    ("Москва г, ул Митинская 17к4", "CMD"),
    ("Москва г, ул Дубравная 41к2", "CMD"),
    ("Москва г, ул Маршала Катукова 6", "CMD"),
    ("Москва г, Строгинский б-р 10к3", "CMD"),
    ("Москва г, ул Маршала Катукова 24к5", "CMD"),
    ("Москва г, ш Волоколамское 71к2", "INVITRO"),
    ("Москва г, ул Габричевского 10к1", "INVITRO"),
    ("Москва г, ул Вишнёвая 13к1 стр. 1", "INVITRO"),
    ("Москва г, б-р Химкинский 9", "INVITRO"),
    ("Москва г, ул Планерная 5", "INVITRO"),
    ("Москва г, б-р Яна Райниса 10", "INVITRO"),
]


def resolve_known(address: str) -> tuple[str, str] | None:
    key = canonical_key(address)
    sig = house_signature(address)
    best: tuple[str, str] | None = None
    best_score = 0.0

    for nav, lab in KNOWN_POINTS:
        candidate_key = canonical_key(nav)
        if key == candidate_key:
            return nav, lab
        candidate_sig = house_signature(nav)
        if sig and candidate_sig and sig != candidate_sig:
            continue
        score = fuzz.ratio(key, candidate_key)
        if score > best_score:
            best_score = score
            best = (nav, lab)

    return best if best_score >= 88 else None
