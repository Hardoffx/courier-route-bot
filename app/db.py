from __future__ import annotations

from datetime import datetime
from pathlib import Path

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
 source TEXT NOT NULL DEFAULT 'PHOTO',
 note TEXT NOT NULL DEFAULT '',
 done INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE CASCADE,
 FOREIGN KEY(known_address_id) REFERENCES known_addresses(id)
);
CREATE INDEX IF NOT EXISTS idx_routes_chat ON routes(chat_id,id);
CREATE INDEX IF NOT EXISTS idx_points_route ON route_points(route_id,position);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


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
        if sig and house_signature(c[2]) != sig:
            continue
        score = fuzz.ratio(key, c[1])
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 94 else None


async def create_route(chat_id: int, route_date: str, image_path: str | None, rows: list[dict]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
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
            if known:
                known_id = known[0]
                nav = known[2]
                await db.execute("UPDATE known_addresses SET last_seen=? WHERE id=?", (now, known_id))
            else:
                key = canonical_key(nav)
                c2 = await db.execute(
                    "INSERT OR IGNORE INTO known_addresses(canonical,nav_address,lab_type,first_seen,last_seen) VALUES(?,?,?,?,?)",
                    (key, nav, lab, now, now),
                )
                known_id = c2.lastrowid or None
            await db.execute(
                """INSERT INTO route_points(route_id,position,known_address_id,raw_text,nav_address,lab_type,source)
                   VALUES(?,?,?,?,?,?,?)""",
                (route_id, idx, known_id, p["raw_text"], nav, lab, p.get("source", "PHOTO")),
            )
        await db.commit()
        return route_id


async def get_points(route_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM route_points WHERE route_id=? ORDER BY position", (route_id,))
        return [dict(x) for x in await c.fetchall()]


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
        c = await db.execute("SELECT * FROM route_points WHERE id=?", (point_id,))
        row = await c.fetchone()
        return dict(row) if row else None


async def mark_done(point_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE route_points SET done=1 WHERE id=?", (point_id,))
        await db.commit()


async def set_note(point_id: int, note: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE route_points SET note=? WHERE id=?", (note, point_id))
        await db.commit()


async def add_manual_point(route_id: int, address: str, position: int | None = None) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        if position is None:
            c = await db.execute("SELECT COALESCE(MAX(position),0)+1 FROM route_points WHERE route_id=?", (route_id,))
            position = (await c.fetchone())[0]
        else:
            await db.execute("UPDATE route_points SET position=position+1 WHERE route_id=? AND position>=?", (route_id, position))
        key = canonical_key(address)
        c = await db.execute("SELECT id,nav_address FROM known_addresses WHERE canonical=?", (key,))
        known = await c.fetchone()
        known_id = known[0] if known else None
        nav = known[1] if known else address
        if not known:
            c = await db.execute(
                "INSERT INTO known_addresses(canonical,nav_address,lab_type,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (key, nav, "UNKNOWN", now, now),
            )
            known_id = c.lastrowid
        c = await db.execute(
            "INSERT INTO route_points(route_id,position,known_address_id,raw_text,nav_address,lab_type,source) VALUES(?,?,?,?,?,?,?)",
            (route_id, position, known_id, address, nav, "UNKNOWN", "MANUAL"),
        )
        await db.commit()
        return c.lastrowid


def compare_routes(old_points: list[dict], new_points: list[dict]) -> dict:
    old = [canonical_key(x["nav_address"]) for x in old_points]
    new = [canonical_key(x["nav_address"]) for x in new_points]
    added = [new_points[i]["nav_address"] for i, k in enumerate(new) if k not in old]
    removed = [old_points[i]["nav_address"] for i, k in enumerate(old) if k not in new]
    return {"same": old == new, "added": added, "removed": removed, "order_changed": set(old) == set(new) and old != new}
