#!/usr/bin/env bash
# Runs ON the server (Ubuntu 24.04). Installs the bot and keeps it running 24/7 via systemd.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/bot}"
cd "$APP_DIR"

echo "== system packages"
sudo apt-get update -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl tar \
  fonts-dejavu-core >/dev/null

PY=python3
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "== python3 is older than 3.12, installing 3.12 with uv"
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  "$UV" python install 3.12
  PY="$("$UV" python find 3.12)"
fi

echo "== Google Chrome (official Google repository) for the Zendriver adapters"
if ! command -v google-chrome-stable >/dev/null; then
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor --yes -o /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
  sudo apt-get update -y -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq google-chrome-stable >/dev/null
fi
google-chrome-stable --version

echo "== virtualenv + dependencies"
"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== database migrations"
mkdir -p data
.venv/bin/alembic upgrade head

echo "== systemd service"
sudo tee /etc/systemd/system/arb-bot.service >/dev/null <<UNIT
[Unit]
Description=Cross-platform arbitrage scanner + Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
Environment=BROWSER_PATH=/usr/bin/google-chrome-stable
ExecStart=$APP_DIR/.venv/bin/python -m app.cli bot
Restart=always
RestartSec=10
MemoryMax=3G

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable arb-bot
sudo systemctl restart arb-bot
sleep 5
sudo systemctl --no-pager --lines=5 status arb-bot || true
echo "== done"
