import asyncio

import aiosqlite

from app import bot
from app import db as db_module
from app.navigation import yandex_url as precise_yandex_url
from app.normalizer import canonical_key
from app.order_preferences import install as install_order_preferences
from app.optimizer_runtime import optimize_remaining_points_live as runtime_optimizer
from app import ui_overrides

YURLOVO_ADDRESS = "деревня Юрлово, 89, Московская область"

_original_init_db = bot.init_db


async def init_db_with_address_fixes():
    await _original_init_db()
    # Keep existing routes and the address catalogue on the courier-confirmed
    # Yurlovo house 89 so maps and optimization use the same exact point.
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

# Saved route order is matched by canonical/fuzzy address instead of only
# known_address_id, so OCR spelling changes cannot lose the learned order.
install_order_preferences(db_module, bot)

# UI handlers resolve this module global at runtime. Replacing it here keeps
# normal daytime optimization on the real clock, but prevents a 23:xx test of
# an already-finished route from treating every LPU as many hours overdue.
ui_overrides.optimize_remaining_points_live = runtime_optimizer
ui_overrides.apply(bot)

if __name__ == "__main__":
    asyncio.run(bot.main())
