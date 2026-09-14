from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

import aiohttp

CACHE_PATH = Path("data/geocache.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
USER_AGENT = "RoutePilot/1.2 (+https://github.com/Hardoffx/courier-route-bot)"


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
    q = re.sub(r"^\s*\d{6}\s*,?\s*", "", address.strip())
    q = re.sub(r"\s+", " ", q).strip(" ,.;")
    replacements = [
        (r"\bМосква\s+г\b", "Москва"),
        (r"\bг\.?\s*Москва\b", "Москва"),
        (r"\bг\.?\s*Красногорск\b", "Красногорск"),
        (r"\bМосковская\s+обл\.?\b", "Московская область"),
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
    return re.sub(r"\s+", " ", q).strip(" ,")


def _query_variants(address: str) -> list[str]:
    base = _clean(address)
    low = base.lower()
    variants: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip(" ,")
        if value and value not in variants:
            variants.append(value)

    # Preserve explicit Moscow-region localities. Never force them into Moscow city.
    if any(x in low for x in ("красногор", "отрадное", "юрлово", "аристово", "солнечногор", "московская область")):
        add(f"{base}, Московская область, Россия")
        add(f"{base}, Россия")
    elif "москва" in low:
        add(f"{base}, Россия")
    else:
        add(f"Москва, {base}, Россия")
        add(f"{base}, Москва, Россия")

    # Geocoders often do better without administrative/service words.
    simplified = re.sub(r"\b(?:территория|тер\.?|район|р-н)\b", " ", base, flags=re.I)
    simplified = re.sub(r"\s+", " ", simplified).strip(" ,")
    if simplified != base:
        if any(x in simplified.lower() for x in ("красногор", "отрадное", "юрлово", "аристово", "солнечногор", "московская область")):
            add(f"{simplified}, Московская область, Россия")
        else:
            add(f"{simplified}, Москва, Россия")
    return variants[:4]


def _valid(lat: float, lon: float) -> bool:
    return 54.8 <= lat <= 56.3 and 36.0 <= lon <= 39.5


def _candidate_coords(features: list[dict]) -> tuple[float, float] | None:
    # Do not trust only the first result: inspect several Moscow-region candidates.
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


async def _geocode_one(session: aiohttp.ClientSession, address: str) -> tuple[float, float] | None:
    variants = _query_variants(address)
    for query in variants:
        coord = await _photon_query(session, query)
        if coord:
            return coord
    # Nominatim is a fallback. Keep requests sequential/rate-limited at caller level.
    for query in variants:
        coord = await _nominatim_query(session, query)
        if coord:
            return coord
        await asyncio.sleep(1.05)
    return None


async def geocode_addresses(addresses: list[str]) -> dict[str, tuple[float, float]]:
    cache = _load_cache()
    result: dict[str, tuple[float, float]] = {}
    missing: list[str] = []
    for address in dict.fromkeys(addresses):
        cached = cache.get(address)
        if isinstance(cached, list) and len(cached) == 2 and _valid(float(cached[0]), float(cached[1])):
            result[address] = (float(cached[0]), float(cached[1]))
        else:
            missing.append(address)

    if not missing:
        return result

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru"}
    connector = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # Photon pass first: cheap enough to parallelize in small batches.
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
                await asyncio.sleep(0.2)

        # Nominatim fallback: one address at a time to respect the public service.
        for address in still_missing:
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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
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
