from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re

import aiosqlite
from rapidfuzz import fuzz

from app.normalizer import canonical_key, house_signature

DB_PATH = Path("data/bot.sqlite3")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS known_addresses(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 canonical TEXT NOT NULL UNIQUE,
 nav_address TEXT NOT NULL,
 lab_type TEXT NOT NULL DEFAULT 'UNKNOWN',
 facility_code TEXT,
 phone TEXT,
 first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routes(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 chat_id INTEGER NOT NULL,
 route_date TEXT NOT NULL,
 image_path TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_points(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 route_id INTEGER NOT NULL,
 position INTEGER NOT NULL,
 known_address_id INTEGER,
 raw_text TEXT NOT NULL,
 nav_address TEXT NOT NULL,
 lab_type TEXT NOT NULL DEFAULT 'UNKNOWN',
 facility_code TEXT,
 window_start TEXT,
 window_end TEXT,
 close_alert_sent INTEGER NOT NULL DEFAULT 0,
 source TEXT NOT NULL DEFAULT 'PHOTO',
 note TEXT NOT NULL DEFAULT '',
 done INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE CASCADE,
 FOREIGN KEY(known_address_id) REFERENCES known_addresses(id)
);
CREATE TABLE IF NOT EXISTS route_order_preferences(
 chat_id INTEGER NOT NULL,
 profile TEXT NOT NULL,
 known_address_id INTEGER NOT NULL,
 rank REAL NOT NULL,
 PRIMARY KEY(chat_id, profile, known_address_id),
 FOREIGN KEY(known_address_id) REFERENCES known_addresses(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_routes_chat ON routes(chat_id,id);
CREATE INDEX IF NOT EXISTS idx_points_route ON route_points(route_id,position);
CREATE INDEX IF NOT EXISTS idx_order_profile ON route_order_preferences(chat_id,profile,rank);
"""


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    cur = await db.execute(f"PRAGMA table_info({table})")
    names = {row[1] for row in await cur.fetchall()}
    if column not in names:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await _ensure_column(db, "known_addresses", "facility_code", "TEXT")
        await _ensure_column(db, "known_addresses", "phone", "TEXT")
        await _ensure_column(db, "route_points", "facility_code", "TEXT")
        await _ensure_column(db, "route_points", "window_start", "TEXT")
        await _ensure_column(db, "route_points", "window_end", "TEXT")
        await _ensure_column(db, "route_points", "close_alert_sent", "INTEGER NOT NULL DEFAULT 0")
        await db.commit()


def route_profile(route_date: str) -> str:
    try:
        day = date.fromisoformat(route_date)
    except ValueError:
        day = date.today()
    return "weekend" if day.weekday() >= 5 else "weekday"


async def _match_known(db, address: str):
    key = canonical_key(address)
    cur = await db.execute("SELECT * FROM known_addresses WHERE canonical=?", (key,))
    row = await cur.fetchone()
    if row:
        return row

    sig = house_signature(address)
    cur = await db.execute("SELECT * FROM known_addresses")
    candidates = await cur.fetchall()
    best = None
    best_score = 0
    for c in candidates:
        if sig and house_signature(c["nav_address"]) != sig:
            continue
        score = fuzz.ratio(key, canonical_key(c["nav_address"]))
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 94 else None


async def _apply_preferred_order(db: aiosqlite.Connection, route_id: int, chat_id: int, route_date: str) -> None:
    profile = route_profile(route_date)
    cur = await db.execute(
        "SELECT known_address_id,rank FROM route_order_preferences WHERE chat_id=? AND profile=?",
        (chat_id, profile),
    )
    prefs = {row[0]: float(row[1]) for row in await cur.fetchall()}
    if not prefs:
        return

    cur = await db.execute(
        "SELECT id,position,known_address_id FROM route_points WHERE route_id=? ORDER BY position",
        (route_id,),
    )
    points = await cur.fetchall()
    if not any(p[2] in prefs for p in points if p[2] is not None):
        return

    keys: list[tuple[float, int, int]] = []
    for i, p in enumerate(points):
        pid, original_pos, known_id = p
        if known_id in prefs:
            keys.append((prefs[known_id], original_pos, pid))
            continue

        prev_rank = next((prefs[q[2]] for q in reversed(points[:i]) if q[2] in prefs), None)
        next_rank = next((prefs[q[2]] for q in points[i + 1:] if q[2] in prefs), None)
        if prev_rank is not None and next_rank is not None and prev_rank < next_rank:
            key = (prev_rank + next_rank) / 2.0 + original_pos / 100000.0
        elif prev_rank is not None:
            key = prev_rank + 500.0 + original_pos / 100000.0
        elif next_rank is not None:
            key = next_rank - 500.0 + original_pos / 100000.0
        else:
            key = 1_000_000.0 + original_pos
        keys.append((key, original_pos, pid))

    ordered = [pid for _, _, pid in sorted(keys)]
    for pos, pid in enumerate(ordered, 1):
        await db.execute("UPDATE route_points SET position=? WHERE id=?", (pos, pid))


async def create_route(chat_id: int, route_date: str, image_path: str | None, rows: list[dict]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "INSERT INTO routes(chat_id,route_date,image_path,created_at) VALUES(?,?,?,?)",
            (chat_id, route_date, image_path, now),
        )
        route_id = c.lastrowid
        for idx, p in enumerate(rows, 1):
            known = await _match_known(db, p["nav_address"])
            known_id = None
            nav = p["nav_address"]
            lab = p.get("lab_type", "UNKNOWN")
            facility = p.get("facility_code")
            if known:
                known_id = known["id"]
                stored_lab = known["lab_type"]
                stored_facility = known["facility_code"]
                if lab == "UNKNOWN" and stored_lab != "UNKNOWN":
                    lab = stored_lab
                if not facility:
                    facility = stored_facility
                await db.execute(
                    "UPDATE known_addresses SET nav_address=?, lab_type=?, facility_code=COALESCE(?,facility_code), last_seen=? WHERE id=?",
                    (nav, lab, facility, now, known_id),
                )
            else:
                key = canonical_key(nav)
                c2 = await db.execute(
                    "INSERT OR IGNORE INTO known_addresses(canonical,nav_address,lab_type,facility_code,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                    (key, nav, lab, facility, now, now),
                )
                known_id = c2.lastrowid or None
                if known_id is None:
                    c3 = await db.execute("SELECT id FROM known_addresses WHERE canonical=?", (key,))
                    row = await c3.fetchone()
                    known_id = row[0] if row else None
            await db.execute(
                """INSERT INTO route_points(
                       route_id,position,known_address_id,raw_text,nav_address,lab_type,facility_code,
                       window_start,window_end,source
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    route_id,
                    idx,
                    known_id,
                    p["raw_text"],
                    nav,
                    lab,
                    facility,
                    p.get("window_start"),
                    p.get("window_end"),
                    p.get("source", "PHOTO"),
                ),
            )
        await _apply_preferred_order(db, route_id, chat_id, route_date)
        await db.commit()
        return route_id


async def get_points(route_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            """SELECT rp.*, ka.phone,
                      COALESCE(rp.facility_code,ka.facility_code) AS effective_facility_code
               FROM route_points rp
               LEFT JOIN known_addresses ka ON ka.id=rp.known_address_id
               WHERE rp.route_id=? ORDER BY rp.position""",
            (route_id,),
        )
        rows = [dict(x) for x in await c.fetchall()]
        for row in rows:
            row["facility_code"] = row.pop("effective_facility_code", None) or row.get("facility_code")
        return rows


async def latest_route(chat_id: int, exclude_route_id: int | None = None):
    sql = "SELECT * FROM routes WHERE chat_id=?"
    args: list = [chat_id]
    if exclude_route_id is not None:
        sql += " AND id<>?"
        args.append(exclude_route_id)
    sql += " ORDER BY id DESC LIMIT 1"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(sql, tuple(args))
        row = await c.fetchone()
        return dict(row) if row else None


async def get_point(point_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            """SELECT rp.*, ka.phone,
                      COALESCE(rp.facility_code,ka.facility_code) AS effective_facility_code
               FROM route_points rp LEFT JOIN known_addresses ka ON ka.id=rp.known_address_id
               WHERE rp.id=?""",
            (point_id,),
        )
        row = await c.fetchone()
        if not row:
            return None
        result = dict(row)
        result["facility_code"] = result.pop("effective_facility_code", None) or result.get("facility_code")
        return result


async def mark_done(point_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE route_points SET done=1 WHERE id=?", (point_id,))
        await db.commit()


async def set_note(point_id: int, note: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE route_points SET note=? WHERE id=?", (note, point_id))
        await db.commit()


def normalize_phone(phone: str) -> str | None:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("7"):
        pass
    else:
        return None
    return "+" + digits


async def set_phone(point_id: int, phone: str) -> str | None:
    normalized = normalize_phone(phone)
    if not normalized:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute("SELECT known_address_id FROM route_points WHERE id=?", (point_id,))
        row = await c.fetchone()
        if not row or row[0] is None:
            return None
        await db.execute("UPDATE known_addresses SET phone=? WHERE id=?", (normalized, row[0]))
        await db.commit()
    return normalized


async def add_manual_point(route_id: int, address: str, position: int | None = None) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        if position is None:
            c = await db.execute("SELECT COALESCE(MAX(position),0)+1 FROM route_points WHERE route_id=?", (route_id,))
            position = (await c.fetchone())[0]
        else:
            await db.execute("UPDATE route_points SET position=position+1 WHERE route_id=? AND position>=?", (route_id, position))
        key = canonical_key(address)
        c = await db.execute("SELECT id,nav_address,lab_type,facility_code FROM known_addresses WHERE canonical=?", (key,))
        known = await c.fetchone()
        known_id = known[0] if known else None
        nav = known[1] if known else address
        lab = known[2] if known else "UNKNOWN"
        facility = known[3] if known else None
        if not known:
            c = await db.execute(
                "INSERT INTO known_addresses(canonical,nav_address,lab_type,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (key, nav, "UNKNOWN", now, now),
            )
            known_id = c.lastrowid
        c = await db.execute(
            """INSERT INTO route_points(route_id,position,known_address_id,raw_text,nav_address,lab_type,facility_code,source)
               VALUES(?,?,?,?,?,?,?,?)""",
            (route_id, position, known_id, address, nav, lab, facility, "MANUAL"),
        )
        await db.commit()
        return c.lastrowid


async def move_point(route_id: int, point_id: int, new_position: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute("SELECT id FROM route_points WHERE route_id=? ORDER BY position", (route_id,))
        ids = [row[0] for row in await c.fetchall()]
        if point_id not in ids:
            raise ValueError("Точка не относится к маршруту")
        ids.remove(point_id)
        new_position = max(1, min(int(new_position), len(ids) + 1))
        ids.insert(new_position - 1, point_id)
        for pos, pid in enumerate(ids, 1):
            await db.execute("UPDATE route_points SET position=? WHERE id=?", (pos, pid))
        await db.commit()
        return new_position


async def save_route_order(route_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute("SELECT chat_id,route_date FROM routes WHERE id=?", (route_id,))
        route = await c.fetchone()
        if not route:
            raise ValueError("Маршрут не найден")
        chat_id, route_date = route
        profile = route_profile(route_date)
        c = await db.execute(
            "SELECT known_address_id FROM route_points WHERE route_id=? AND known_address_id IS NOT NULL ORDER BY position",
            (route_id,),
        )
        known_ids = [row[0] for row in await c.fetchall()]
        await db.execute("DELETE FROM route_order_preferences WHERE chat_id=? AND profile=?", (chat_id, profile))
        for rank, known_id in enumerate(known_ids, 1):
            await db.execute(
                "INSERT INTO route_order_preferences(chat_id,profile,known_address_id,rank) VALUES(?,?,?,?)",
                (chat_id, profile, known_id, rank * 1000.0),
            )
        await db.commit()
        return profile


async def pending_deadline_alerts(now: datetime, minutes: int = 40) -> list[dict]:
    """Claim deadline alerts atomically so the watcher sends each alert once."""
    today = now.date().isoformat()
    results: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            """SELECT rp.*, r.chat_id, r.route_date,
                      COALESCE(rp.facility_code,ka.facility_code) AS effective_facility_code,
                      (SELECT MIN(x.position) FROM route_points x
                       WHERE x.route_id=rp.route_id AND x.done=0) AS current_position
               FROM route_points rp
               JOIN routes r ON r.id=rp.route_id
               LEFT JOIN known_addresses ka ON ka.id=rp.known_address_id
               WHERE r.route_date=? AND rp.done=0 AND rp.window_end IS NOT NULL AND rp.close_alert_sent=0
                 AND r.id IN (SELECT MAX(id) FROM routes WHERE route_date=? GROUP BY chat_id)
               ORDER BY r.chat_id,rp.position""",
            (today, today),
        )
        rows = [dict(row) for row in await c.fetchall()]
        by_route: dict[int, list[dict]] = {}
        for row in rows:
            row["facility_code"] = row.pop("effective_facility_code", None) or row.get("facility_code")
            by_route.setdefault(row["route_id"], []).append(row)

        for route_id, points in by_route.items():
            for p in points:
                if p["position"] <= (p.get("current_position") or p["position"]):
                    continue
                try:
                    hour, minute = map(int, p["window_end"].split(":"))
                    closes = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                except (ValueError, AttributeError):
                    continue
                delta = int((closes - now).total_seconds() // 60)
                if 0 <= delta <= minutes:
                    results.append({**p, "minutes_left": delta})
                    await db.execute("UPDATE route_points SET close_alert_sent=1 WHERE id=?", (p["id"],))
        await db.commit()
    return results


def compare_routes(old_points: list[dict], new_points: list[dict]) -> dict:
    old = [canonical_key(x["nav_address"]) for x in old_points]
    new = [canonical_key(x["nav_address"]) for x in new_points]
    added = [new_points[i]["nav_address"] for i, k in enumerate(new) if k not in old]
    removed = [old_points[i]["nav_address"] for i, k in enumerate(old) if k not in new]
    return {"same": old == new, "added": added, "removed": removed, "order_changed": set(old) == set(new) and old != new}
