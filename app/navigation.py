from __future__ import annotations

import re
from urllib.parse import quote_plus


YURLOVO_QUERY = "деревня Юрлово, 87, Московская область, Россия"


def navigation_query(address: str) -> str:
    """Return a precise map-search query for the courier point."""
    q = re.sub(r"\s+", " ", str(address or "").strip())

    # This route-sheet locality is ambiguous in Yandex: a long administrative
    # string can select the whole Solnechnogorsk district instead of the house.
    # The courier-confirmed destination is exactly village Yurlovo, house 87.
    if re.search(r"\bЮрлово\b", q, flags=re.I):
        return YURLOVO_QUERY

    return q


def yandex_url(address: str) -> str:
    return "https://yandex.ru/maps/?text=" + quote_plus(navigation_query(address))
