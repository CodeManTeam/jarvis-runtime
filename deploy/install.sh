#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/jarvis
sudo useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin jarvis 2>/dev/null || true
sudo mkdir -p "$APP_DIR"
sudo chown -R "$USER":"$USER" "$APP_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
sudo cp "$APP_DIR/deploy/jarvis.service" /etc/systemd/system/jarvis.service
sudo chown -R jarvis:jarvis "$APP_DIR"
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl restart jarvis
sudo systemctl --no-pager --full status jarvis

