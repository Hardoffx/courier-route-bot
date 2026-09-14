import asyncio

from app import bot
from app.ui_overrides import apply
from app.live_optimizer_ui import apply as apply_live_optimizer

apply(bot)
apply_live_optimizer(bot)

if __name__ == "__main__":
    asyncio.run(bot.main())
