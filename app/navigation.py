from __future__ import annotations

import re
from urllib.parse import quote_plus


def navigation_query(address: str) -> str:
    """Return a map-search query with known locality ambiguities removed."""
    q = re.sub(r"\s+", " ", str(address or "").strip())

    # Yandex can interpret bare "Юрлово <house>" as the whole locality/area.
    # Preserve the actual house number and explicitly say that this is a village.
    if re.search(r"\bЮрлово\b", q, flags=re.I):
        q = re.sub(r"\b(?:д\.?|деревня)?\s*Юрлово\b", "деревня Юрлово", q, count=1, flags=re.I)
        # Remove administrative noise that can make the search less precise.
        q = re.sub(r"\bСолнечногорск(?:ий|ого)?\s+(?:р-н|район)\b,?\s*", "", q, flags=re.I)
        q = re.sub(r"\bМосковская\s+обл(?:асть)?\b,?\s*", "Московская область, ", q, flags=re.I)
        q = re.sub(r"\s*,\s*,+", ", ", q)
        q = re.sub(r"\s+", " ", q).strip(" ,")

    return q


def yandex_url(address: str) -> str:
    return "https://yandex.ru/maps/?text=" + quote_plus(navigation_query(address))
