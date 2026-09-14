import asyncio

import aiosqlite

from app import bot
from app import db as db_module
from app.navigation import yandex_url as precise_yandex_url
from app.normalizer import canonical_key
from app.ui_overrides import apply

YURLOVO_ADDRESS = "деревня Юрлово, 87, Московская область"

_original_init_db = bot.init_db


async def init_db_with_address_fixes():
    await _original_init_db()
    # Correct already-saved routes too, not only newly OCRed ones. This makes
    # the current route card, map link and optimizer all use the same house.
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute(
            "UPDATE route_points SET nav_address=? WHERE lower(nav_address) LIKE '%юрлово%'",
            (YURLOVO_ADDRESS,),
        )
        cur = await db.execute(
            "SELECT id FROM known_addresses WHERE lower(nav_address) LIKE '%юрлово%' ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            try:
                await db.execute(
                    "UPDATE known_addresses SET nav_address=?, canonical=? WHERE id=?",
                    (YURLOVO_ADDRESS, canonical_key(YURLOVO_ADDRESS), row[0]),
                )
            except aiosqlite.IntegrityError:
                # If a corrected canonical row already exists, keeping the old
                # known-address row is harmless; route_points are fixed above.
                await db.rollback()
                await db.execute(
                    "UPDATE route_points SET nav_address=? WHERE lower(nav_address) LIKE '%юрлово%'",
                    (YURLOVO_ADDRESS,),
                )
        await db.commit()


bot.init_db = init_db_with_address_fixes
bot.yandex_url = precise_yandex_url
apply(bot)

if __name__ == "__main__":
    asyncio.run(bot.main())
