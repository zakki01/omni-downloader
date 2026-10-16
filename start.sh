#!/bin/sh
set -e

# Ensure downloads directory exists
mkdir -p /app/downloads

APP_PORT="${PORT:-8000}"
echo "[CONTAINER] Starting Omni Downloader on 0.0.0.0:${APP_PORT}..."
exec uvicorn app:app --host 0.0.0.0 --port "$APP_PORT"
