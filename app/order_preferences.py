from __future__ import annotations

from rapidfuzz import fuzz

from app.normalizer import canonical_key, house_signature


def _match_rank(address: str, preferred: list[tuple[str, float]]) -> float | None:
    key = canonical_key(address)
    sig = house_signature(address)

    for pref_address, rank in preferred:
        if key == canonical_key(pref_address):
            return rank

    best_rank = None
    best_score = 0.0
    for pref_address, rank in preferred:
        pref_sig = house_signature(pref_address)
        if sig and pref_sig and sig != pref_sig:
            continue
        score = fuzz.ratio(key, canonical_key(pref_address))
        if score > best_score:
            best_score = score
            best_rank = rank
    return best_rank if best_score >= 88 else None


async def apply_preferred_order(db, route_id: int, chat_id: int, route_date: str, route_profile) -> None:
    """Apply a saved route order by address identity, not fragile database IDs."""
    profile = route_profile(route_date)

    # v2 stores the canonical address itself, so OCR/new known_address IDs do not
    # destroy the user's learned order.
    await db.execute(
        """CREATE TABLE IF NOT EXISTS route_order_preferences_v2(
               chat_id INTEGER NOT NULL,
               profile TEXT NOT NULL,
               canonical TEXT NOT NULL,
               nav_address TEXT NOT NULL,
               rank REAL NOT NULL,
               PRIMARY KEY(chat_id, profile, canonical)
           )"""
    )
    cur = await db.execute(
        "SELECT nav_address,rank FROM route_order_preferences_v2 WHERE chat_id=? AND profile=? ORDER BY rank",
        (chat_id, profile),
    )
    preferred = [(row[0], float(row[1])) for row in await cur.fetchall()]

    # Backward compatibility: recover orders the user already saved with the old
    # known_address_id based system. This means today's saved order is not lost.
    if not preferred:
        cur = await db.execute(
            """SELECT ka.nav_address,rop.rank
               FROM route_order_preferences rop
               JOIN known_addresses ka ON ka.id=rop.known_address_id
               WHERE rop.chat_id=? AND rop.profile=?
               ORDER BY rop.rank""",
            (chat_id, profile),
        )
        preferred = [(row[0], float(row[1])) for row in await cur.fetchall()]

    if not preferred:
        return

    cur = await db.execute(
        "SELECT id,position,nav_address FROM route_points WHERE route_id=? ORDER BY position",
        (route_id,),
    )
    points = await cur.fetchall()
    matched_ranks = [_match_rank(row[2], preferred) for row in points]
    if not any(rank is not None for rank in matched_ranks):
        return

    sort_keys: list[tuple[float, int, int]] = []
    for i, row in enumerate(points):
        pid, original_pos, _address = row
        rank = matched_ranks[i]
        if rank is not None:
            sort_keys.append((rank, original_pos, pid))
            continue

        # New/changed addresses stay close to their original neighbors instead
        # of being dumped at the end of the learned route.
        prev_rank = next((r for r in reversed(matched_ranks[:i]) if r is not None), None)
        next_rank = next((r for r in matched_ranks[i + 1:] if r is not None), None)
        if prev_rank is not None and next_rank is not None and prev_rank < next_rank:
            key = (prev_rank + next_rank) / 2.0 + original_pos / 100000.0
        elif prev_rank is not None:
            key = prev_rank + 500.0 + original_pos / 100000.0
        elif next_rank is not None:
            key = next_rank - 500.0 + original_pos / 100000.0
        else:
            key = 1_000_000.0 + original_pos
        sort_keys.append((key, original_pos, pid))

    for position, (_key, _original, pid) in enumerate(sorted(sort_keys), 1):
        await db.execute("UPDATE route_points SET position=? WHERE id=?", (position, pid))


async def save_route_order_v2(route_id: int, db_path, route_profile) -> str:
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        route_cur = await db.execute("SELECT chat_id,route_date FROM routes WHERE id=?", (route_id,))
        route = await route_cur.fetchone()
        if not route:
            raise ValueError("Маршрут не найден")
        chat_id, route_date = route
        profile = route_profile(route_date)

        await db.execute(
            """CREATE TABLE IF NOT EXISTS route_order_preferences_v2(
                   chat_id INTEGER NOT NULL,
                   profile TEXT NOT NULL,
                   canonical TEXT NOT NULL,
                   nav_address TEXT NOT NULL,
                   rank REAL NOT NULL,
                   PRIMARY KEY(chat_id, profile, canonical)
               )"""
        )
        cur = await db.execute(
            "SELECT nav_address FROM route_points WHERE route_id=? ORDER BY position",
            (route_id,),
        )
        addresses = [row[0] for row in await cur.fetchall()]
        await db.execute(
            "DELETE FROM route_order_preferences_v2 WHERE chat_id=? AND profile=?",
            (chat_id, profile),
        )
        for position, address in enumerate(addresses, 1):
            await db.execute(
                """INSERT OR REPLACE INTO route_order_preferences_v2
                   (chat_id,profile,canonical,nav_address,rank) VALUES(?,?,?,?,?)""",
                (chat_id, profile, canonical_key(address), address, position * 1000.0),
            )
        await db.commit()
        return profile


def install(db_module, bot_module) -> None:
    async def robust_apply(db, route_id: int, chat_id: int, route_date: str):
        await apply_preferred_order(db, route_id, chat_id, route_date, db_module.route_profile)

    async def robust_save(route_id: int) -> str:
        return await save_route_order_v2(route_id, db_module.DB_PATH, db_module.route_profile)

    # create_route resolves this global from app.db at runtime.
    db_module._apply_preferred_order = robust_apply
    db_module.save_route_order = robust_save
    # bot.py imported save_route_order directly, so replace that reference too.
    bot_module.save_route_order = robust_save
