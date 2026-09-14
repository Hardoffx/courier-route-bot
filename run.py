import asyncio

from app import bot
from app.ui_overrides import apply

apply(bot)

if __name__ == "__main__":
    asyncio.run(bot.main())
