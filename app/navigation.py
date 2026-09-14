from __future__ import annotations

import re
from urllib.parse import quote_plus


YURLOVO_QUERY = "деревня Юрлово, 89, Московская область, Россия"


def navigation_query(address: str) -> str:
    """Return a precise map-search query for the courier point."""
    q = re.sub(r"\s+", " ", str(address or "").strip())

    # Yandex can interpret the long administrative form as the whole
    # Solnechnogorsk district. For this route point use the exact village house.
    if re.search(r"\bЮрлово\b", q, flags=re.I):
        return YURLOVO_QUERY

    return q


def yandex_url(address: str) -> str:
    return "https://yandex.ru/maps/?text=" + quote_plus(navigation_query(address))
