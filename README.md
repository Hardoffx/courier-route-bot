# Courier Route Bot

Telegram-бот для автоматического создания ежедневного маршрута курьера по фотографии маршрутного листа.

## MVP

- фотография маршрутного листа в Telegram;
- адреса извлекаются строго сверху вниз;
- тип точки определяется по цвету строки: INVITRO / CMD / другая лаборатория;
- адрес нормализуется под Яндекс Карты;
- известные адреса сохраняются в SQLite и повторно используются;
- новый маршрут сравнивается с предыдущим: добавленные/убранные точки и изменение порядка;
- последовательный режим `точка → Яндекс Карты → выполнено → следующая`;
- одноразовая заметка к точке для расходников, документов и поручений;
- дополнительные точки можно добавлять в течение дня командой `/add` или кнопкой.

## Быстрый запуск Ubuntu 24.04

После слияния MVP в `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/Hardoffx/courier-route-bot/main/deploy/install.sh -o /tmp/install-courier-bot.sh
sudo bash /tmp/install-courier-bot.sh
```

Установщик сам поставит Python, Tesseract OCR с русским языком, создаст виртуальное окружение и systemd-сервис. Он попросит только `BOT_TOKEN` от BotFather.

Проверка:

```bash
systemctl status courier-route-bot
journalctl -u courier-route-bot -f
```

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Секреты (`.env`) и рабочая база (`data/`) в Git не попадают.
