#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/kis-ict}"
KIS_CONFIG_DIR="${KIS_CONFIG_DIR:-$HOME/KIS/config}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "Missing app directory: $APP_DIR"
  echo "Clone the repository first, for example:"
  echo "  sudo mkdir -p $APP_DIR && sudo chown \$USER:\$USER $APP_DIR"
  echo "  git clone https://github.com/team-muel/koreainvestment_open_trading.git $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-plugin
  sudo systemctl enable --now docker
fi

if ! command -v crontab >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y cron
  sudo systemctl enable --now cron
fi

mkdir -p "$KIS_CONFIG_DIR" logs strategy_builder/data
chmod 700 "$(dirname "$KIS_CONFIG_DIR")" "$KIS_CONFIG_DIR" || true

if [[ ! -f .env ]]; then
  cp .env.example .env
  if grep -q '^KIS_CONFIG_DIR=' .env; then
    sed -i "s|^KIS_CONFIG_DIR=.*|KIS_CONFIG_DIR=$KIS_CONFIG_DIR|" .env
  fi
  echo "Created .env from .env.example. Edit it before live use:"
  echo "  nano $APP_DIR/.env"
fi

if [[ ! -f "$KIS_CONFIG_DIR/kis_devlp.yaml" ]]; then
  echo "WARNING: $KIS_CONFIG_DIR/kis_devlp.yaml does not exist yet."
  echo "Place your KIS config there and run:"
  echo "  chmod 600 $KIS_CONFIG_DIR/kis_devlp.yaml"
fi

docker compose build
docker compose up -d backend frontend

crontab deploy/cron/ict-crontab

echo "Deployment bootstrap complete."
echo "Check containers: docker compose ps"
echo "Check cron: crontab -l"
echo "Check logs: tail -f $APP_DIR/logs/cron.log"
