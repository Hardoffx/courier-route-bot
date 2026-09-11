#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/courier-route-bot
REPO_URL=https://github.com/Hardoffx/courier-route-bot.git
SERVICE=/etc/systemd/system/courier-route-bot.service

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh"
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-rus libgl1

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --all --prune
  git -C "$APP_DIR" checkout main
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
mkdir -p "$APP_DIR/data/uploads"

if [[ ! -f "$APP_DIR/.env" ]]; then
  read -r -p "Telegram BOT_TOKEN: " BOT_TOKEN
  if [[ -z "$BOT_TOKEN" ]]; then
    echo "BOT_TOKEN cannot be empty"
    exit 1
  fi
  printf 'BOT_TOKEN=%s\nTIMEZONE=Europe/Moscow\n' "$BOT_TOKEN" > "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

cat > "$SERVICE" <<'EOF'
[Unit]
Description=Courier Route Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/courier-route-bot
EnvironmentFile=/opt/courier-route-bot/.env
ExecStart=/opt/courier-route-bot/.venv/bin/python /opt/courier-route-bot/run.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now courier-route-bot
sleep 2
systemctl --no-pager --full status courier-route-bot || true

echo
echo "Installed. Logs: journalctl -u courier-route-bot -f"
