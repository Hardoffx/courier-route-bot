from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

import aiohttp

CACHE_PATH = Path("data/geocache.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
USER_AGENT = "RoutePilot/1.4 (+https://github.com/Hardoffx/courier-route-bot)"
YURLOVO_QUERY = "деревня Юрлово, 89, Московская область, Россия"


def _load_cache() -> dict[str, list[float]]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _clean(address: str) -> str:
    q = re.sub(r"^\s*\d{6}\s*,?\s*", "", str(address or "").strip())
    q = re.sub(r"\s+", " ", q).strip(" ,.;")
    replacements = [
        (r"\bМосква\s+г\b", "Москва"),
        (r"\bг\.?\s*Москва\b", "Москва"),
        (r"\bг\.?\s*Красногорск\b", "Красногорск"),
        (r"\bМосковская\s+обл\.?\b", "Московская область"),
        (r"\bСолнечногорск(?:ий|ого)?\s+(?:р-н|район)\b,?\s*", ""),
        (r"\bМитинский\s+3-й\s+пер\.?\b", "3-й Митинский переулок"),
        (r"\bул\.?\s+", "улица "),
        (r"\bб-р\s+", "бульвар "),
        (r"\bпер\.?\s+", "переулок "),
        (r"\bпр-д\s+", "проезд "),
        (r"\bш\.?\s+", "шоссе "),
        (r"\bп\.?\s+", "поселок "),
        (r"(?<=\d)к(?=\d+\b)", " корпус "),
        (r"(?<=\d)стр\.?\s*(?=\d+\b)", " строение "),
    ]
    for pattern, repl in replacements:
        q = re.sub(pattern, repl, q, flags=re.I)
    q = re.sub(r"\s*,\s*", ", ", q)
    return re.sub(r"\s+", " ", q).strip(" ,")


def _street_first_variant(base: str) -> str | None:
    """Reorder Moscow addresses to the form geocoders usually understand best."""
    value = re.sub(r"^Москва\s*,\s*", "", base, flags=re.I)
    m = re.match(
        r"(.+?(?:улица|бульвар|переулок|проезд|шоссе)|(?:улица|бульвар|переулок|проезд|шоссе)\s+.+?)\s+(\d+[А-Яа-яA-Za-z]?(?:\s+(?:корпус|строение)\s+\d+)?)$",
        value,
        flags=re.I,
    )
    if not m:
        # Most normalized rows are 'улица NAME 19 корпус 1'.
        m = re.match(
            r"((?:улица|бульвар|переулок|проезд|шоссе)\s+.+?)\s+(\d+[А-Яа-яA-Za-z]?(?:\s+(?:корпус|строение)\s+\d+)?)$",
            value,
            flags=re.I,
        )
    if not m:
        return None
    return f"{m.group(1)}, {m.group(2)}, Москва, Россия"


def _query_variants(address: str) -> list[str]:
    base = _clean(address)
    low = base.lower()

    if "юрлово" in low:
        return [YURLOVO_QUERY, "деревня Юрлово 89, Московская область, Россия"]

    variants: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        value = re.sub(r"\s+", " ", value).strip(" ,")
        if value and value not in variants:
            variants.append(value)

    # First try a street/house-first query. This is materially more reliable
    # for Russian house numbers than the OCR order 'Москва, улица ..., 19'.
    add(_street_first_variant(base))

    if any(x in low for x in ("красногор", "отрадное", "аристово", "солнечногор", "московская область")):
        add(f"{base}, Московская область, Россия")
        add(f"{base}, Россия")
    elif "москва" in low:
        add(f"{base}, Россия")
    else:
        add(f"{base}, Москва, Россия")
        add(f"Москва, {base}, Россия")

    simplified = re.sub(r"\b(?:территория|тер\.?|район|р-н)\b", " ", base, flags=re.I)
    simplified = re.sub(r"\s+", " ", simplified).strip(" ,")
    if simplified != base:
        if any(x in simplified.lower() for x in ("красногор", "отрадное", "аристово", "солнечногор", "московская область")):
            add(f"{simplified}, Московская область, Россия")
        else:
            add(f"{simplified}, Москва, Россия")
    return variants[:5]


def _valid(lat: float, lon: float) -> bool:
    # Moscow + Moscow Oblast working area, deliberately tighter than all Russia.
    return 54.8 <= lat <= 56.3 and 36.0 <= lon <= 39.5


def _candidate_coords(features: list[dict]) -> tuple[float, float] | None:
    for feature in features[:5]:
        try:
            lon, lat = feature["geometry"]["coordinates"]
            lat, lon = float(lat), float(lon)
            if _valid(lat, lon):
                return lat, lon
        except Exception:
            continue
    return None


async def _photon_query(session: aiohttp.ClientSession, query: str) -> tuple[float, float] | None:
    url = f"https://photon.komoot.io/api/?q={quote(query)}&limit=5&lang=ru"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        return _candidate_coords(data.get("features") or [])
    except Exception:
        return None


async def _arcgis_query(session: aiohttp.ClientSession, query: str) -> tuple[float, float] | None:
    """Second independent geocoder; public ArcGIS World Geocoding endpoint."""
    url = (
        "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
        f"findAddressCandidates?SingleLine={quote(query)}&f=json&countryCode=RUS&maxLocations=5&outFields=Match_addr,Addr_type"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        for item in data.get("candidates") or []:
            try:
                loc = item.get("location") or {}
                lat, lon = float(loc["y"]), float(loc["x"])
                if _valid(lat, lon):
                    return lat, lon
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _nominatim_query(session: aiohttp.ClientSession, query: str) -> tuple[float, float] | None:
    url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&countrycodes=ru&q={quote(query)}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        for item in data[:5]:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
                if _valid(lat, lon):
                    return lat, lon
            except Exception:
                continue
    except Exception:
        pass
    return None


async def geocode_addresses(addresses: list[str]) -> dict[str, tuple[float, float]]:
    cache = _load_cache()
    result: dict[str, tuple[float, float]] = {}
    missing: list[str] = []
    for address in dict.fromkeys(addresses):
        force_refresh = "юрлово" in address.lower()
        cached = None if force_refresh else cache.get(address)
        if isinstance(cached, list) and len(cached) == 2 and _valid(float(cached[0]), float(cached[1])):
            result[address] = (float(cached[0]), float(cached[1]))
        else:
            missing.append(address)

    if not missing:
        return result

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru"}
    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # Pass 1: Photon, batched.
        still_missing: list[str] = []
        for start in range(0, len(missing), 4):
            batch = missing[start:start + 4]

            async def photon_only(address: str):
                for query in _query_variants(address):
                    coord = await _photon_query(session, query)
                    if coord:
                        return coord
                return None

            values = await asyncio.gather(*[photon_only(a) for a in batch])
            for address, coord in zip(batch, values):
                if coord:
                    result[address] = coord
                    cache[address] = [coord[0], coord[1]]
                else:
                    still_missing.append(address)
            if start + 4 < len(missing):
                await asyncio.sleep(0.15)

        # Pass 2: ArcGIS. It is independent from OSM/Photon and tends to resolve
        # Russian street + house combinations that the OSM stack misses.
        after_arcgis: list[str] = []
        for start in range(0, len(still_missing), 4):
            batch = still_missing[start:start + 4]

            async def arcgis_only(address: str):
                for query in _query_variants(address):
                    coord = await _arcgis_query(session, query)
                    if coord:
                        return coord
                return None

            values = await asyncio.gather(*[arcgis_only(a) for a in batch])
            for address, coord in zip(batch, values):
                if coord:
                    result[address] = coord
                    cache[address] = [coord[0], coord[1]]
                else:
                    after_arcgis.append(address)
            if start + 4 < len(still_missing):
                await asyncio.sleep(0.15)

        # Pass 3: Nominatim, deliberately sequential to respect public limits.
        for address in after_arcgis:
            coord = None
            for query in _query_variants(address):
                coord = await _nominatim_query(session, query)
                if coord:
                    break
                await asyncio.sleep(1.05)
            if coord:
                result[address] = coord
                cache[address] = [coord[0], coord[1]]
            await asyncio.sleep(1.05)

    _save_cache(cache)
    return result


async def osrm_duration_matrix(coords: list[tuple[float, float]]) -> list[list[float | None]] | None:
    if len(coords) < 2:
        return [[0.0]] if coords else []
    coord_text = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    url = f"https://router.project-osrm.org/table/v1/driving/{coord_text}?annotations=duration"
    try:
        headers = {"User-Agent": USER_AGENT}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        durations = data.get("durations")
        if not durations:
            return None
        return [[None if value is None else float(value) / 60.0 for value in row] for row in durations]
    except Exception:
        return None


async def road_matrix_for_points(points: list[dict]) -> tuple[list[list[float | None]] | None, int]:
    addresses = [str(p.get("nav_address") or "").strip() for p in points]
    mapping = await geocode_addresses(addresses)
    known_indexes = [i for i, address in enumerate(addresses) if address in mapping]
    geocoded = len(known_indexes)
    if geocoded < 2:
        return None, geocoded

    coords = [mapping[addresses[i]] for i in known_indexes]
    partial = await osrm_duration_matrix(coords)
    if partial is None:
        return None, geocoded

    n = len(points)
    matrix: list[list[float | None]] = [[None for _ in range(n)] for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 0.0
    for a, original_i in enumerate(known_indexes):
        for b, original_j in enumerate(known_indexes):
            matrix[original_i][original_j] = partial[a][b]
    return matrix, geocoded
