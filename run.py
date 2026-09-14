import asyncio

from app import bot
from app.navigation import yandex_url as precise_yandex_url
from app.ui_overrides import apply

bot.yandex_url = precise_yandex_url
apply(bot)

if __name__ == "__main__":
    asyncio.run(bot.main())
