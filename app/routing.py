from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import aiohttp

CACHE_PATH = Path("data/geocache.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
USER_AGENT = "RoutePilot/1.0 (+https://github.com/Hardoffx/courier-route-bot)"


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


async def _photon_geocode(session: aiohttp.ClientSession, address: str) -> tuple[float, float] | None:
    # Moscow/oblast context materially improves ambiguous street matches.
    q = address
    if "москва" not in q.lower() and "москов" not in q.lower() and "красногор" not in q.lower():
        q = f"{q}, Москва, Россия"
    url = f"https://photon.komoot.io/api/?q={quote(q)}&limit=1&lang=ru"
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
        # Guard against wildly wrong matches outside Moscow region.
        if not (54.8 <= lat <= 56.3 and 36.0 <= lon <= 39.5):
            return None
        return lat, lon
    except Exception:
        return None


async def geocode_addresses(addresses: list[str]) -> dict[str, tuple[float, float]]:
    cache = _load_cache()
    result: dict[str, tuple[float, float]] = {}
    missing: list[str] = []
    for address in addresses:
        cached = cache.get(address)
        if isinstance(cached, list) and len(cached) == 2:
            result[address] = (float(cached[0]), float(cached[1]))
        else:
            missing.append(address)

    if missing:
        headers = {"User-Agent": USER_AGENT}
        connector = aiohttp.TCPConnector(limit=4)
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            # Small parallel batches keep first-time optimization reasonably fast.
            for start in range(0, len(missing), 4):
                batch = missing[start:start + 4]
                values = await asyncio.gather(*[_photon_geocode(session, a) for a in batch])
                for address, coord in zip(batch, values):
                    if coord:
                        result[address] = coord
                        cache[address] = [coord[0], coord[1]]
                if start + 4 < len(missing):
                    await asyncio.sleep(0.25)
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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=18)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        durations = data.get("durations")
        if not durations:
            return None
        return [
            [None if value is None else float(value) / 60.0 for value in row]
            for row in durations
        ]
    except Exception:
        return None


async def road_matrix_for_points(points: list[dict]) -> tuple[list[list[float | None]] | None, int]:
    """Build a matrix aligned with points; return matrix and geocoded count."""
    addresses = [str(p.get("nav_address") or "").strip() for p in points]
    mapping = await geocode_addresses(addresses)
    if len(mapping) != len(addresses):
        return None, len(mapping)
    coords = [mapping[a] for a in addresses]
    matrix = await osrm_duration_matrix(coords)
    return matrix, len(mapping)
