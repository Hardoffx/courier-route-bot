import asyncio

import aiosqlite

from app import bot
from app import db as db_module
from app.navigation import yandex_url as precise_yandex_url
from app.normalizer import canonical_key
from app.order_preferences import install as install_order_preferences
from app.route_timing import install as install_route_timing
from app.optimizer_runtime import optimize_remaining_points_live as runtime_optimizer
from app import ui_overrides

YURLOVO_ADDRESS = "деревня Юрлово, 89, Московская область"

_original_init_db = bot.init_db


async def init_db_with_address_fixes():
    await _original_init_db()
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
                await db.rollback()
                await db.execute(
                    "UPDATE route_points SET nav_address=? WHERE lower(nav_address) LIKE '%юрлово%'",
                    (YURLOVO_ADDRESS,),
                )
        await db.commit()


bot.init_db = init_db_with_address_fixes
bot.yandex_url = precise_yandex_url

# Keep learned weekday/weekend ordering stable even when OCR spelling varies.
install_order_preferences(db_module, bot)

# Remember the actual start of a route from the first completed point, reuse it
# when the same day's sheet is uploaded again, and learn typical weekday/weekend
# starts over time instead of assuming a fixed 12:00.
install_route_timing(db_module, bot)

# UI handlers resolve this module global at runtime.
ui_overrides.optimize_remaining_points_live = runtime_optimizer
ui_overrides.apply(bot)

if __name__ == "__main__":
    asyncio.run(bot.main())
