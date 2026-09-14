from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import aiohttp

CACHE_PATH = Path("data/geocache.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
USER_AGENT = "RoutePilot/1.1 (+https://github.com/Hardoffx/courier-route-bot)"


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


def _query(address: str) -> str:
    q = address.strip()
    low = q.lower()
    if not any(x in low for x in ("москва", "москов", "красногор", "солнечногор")):
        q = f"{q}, Москва, Россия"
    return q


def _valid(lat: float, lon: float) -> bool:
    return 54.8 <= lat <= 56.3 and 36.0 <= lon <= 39.5


async def _photon_geocode(session: aiohttp.ClientSession, address: str) -> tuple[float, float] | None:
    url = f"https://photon.komoot.io/api/?q={quote(_query(address))}&limit=1&lang=ru"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        features = data.get("features") or []
        if not features:
            return None
        lon, lat = features[0]["geometry"]["coordinates"]
        lat, lon = float(lat), float(lon)
        return (lat, lon) if _valid(lat, lon) else None
    except Exception:
        return None


async def _nominatim_geocode(session: aiohttp.ClientSession, address: str) -> tuple[float, float] | None:
    url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=ru&q={quote(_query(address))}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        if not data:
            return None
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
        return (lat, lon) if _valid(lat, lon) else None
    except Exception:
        return None


async def geocode_addresses(addresses: list[str]) -> dict[str, tuple[float, float]]:
    cache = _load_cache()
    result: dict[str, tuple[float, float]] = {}
    missing: list[str] = []
    for address in dict.fromkeys(addresses):
        cached = cache.get(address)
        if isinstance(cached, list) and len(cached) == 2:
            result[address] = (float(cached[0]), float(cached[1]))
        else:
            missing.append(address)

    if not missing:
        return result

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru"}
    connector = aiohttp.TCPConnector(limit=4)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        still_missing: list[str] = []
        for start in range(0, len(missing), 4):
            batch = missing[start:start + 4]
            values = await asyncio.gather(*[_photon_geocode(session, a) for a in batch])
            for address, coord in zip(batch, values):
                if coord:
                    result[address] = coord
                    cache[address] = [coord[0], coord[1]]
                else:
                    still_missing.append(address)
            if start + 4 < len(missing):
                await asyncio.sleep(0.2)

        for i, address in enumerate(still_missing):
            coord = await _nominatim_geocode(session, address)
            if coord:
                result[address] = coord
                cache[address] = [coord[0], coord[1]]
            if i + 1 < len(still_missing):
                await asyncio.sleep(1.05)

    _save_cache(cache)
    return result


async def osrm_duration_matrix(coords: list[tuple[float, float]]) -> list[list[float | None]] | None:
    """Return car travel durations in minutes using OSRM road routing."""
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
    """Build a usable full matrix even when one or more addresses are unknown."""
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
