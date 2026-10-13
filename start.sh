#!/bin/bash
set -e

echo "============================================================"
echo "Omni Downloader - Production Container Boot"
echo "============================================================"

# Ensure directories exist
mkdir -p /app/logs /app/downloads

POT_LOG="/app/logs/pot_provider.log"
POT_DIR=""

if [ -d "/opt/pot-provider" ]; then
    POT_DIR="/opt/pot-provider"
elif [ -d "/app/pot-provider" ]; then
    POT_DIR="/app/pot-provider"
fi

if [ -n "$POT_DIR" ] && { [ -f "$POT_DIR/build/main.js" ] || [ -f "$POT_DIR/src/main.ts" ]; }; then
    echo "[CONTAINER] Starting BGUtil PO Token Provider on 127.0.0.1:4416..."
    if command -v node >/dev/null 2>&1 && [ -f "$POT_DIR/build/main.js" ]; then
        echo "[CONTAINER] Runtime: Node.js ($(node -v))"
        cd "$POT_DIR"
        node build/main.js -H 127.0.0.1 -p 4416 >> "$POT_LOG" 2>&1 &
        POT_PID=$!
        export BGUTIL_PID="$POT_PID"
        export BGUTIL_CMD="node $POT_DIR/build/main.js -H 127.0.0.1 -p 4416"
        cd /app
    elif command -v deno >/dev/null 2>&1 && [ -f "$POT_DIR/src/main.ts" ]; then
        echo "[CONTAINER] Runtime: Deno ($(deno -V))"
        cd "$POT_DIR"
        deno run --allow-env --allow-net --allow-ffi --allow-read src/main.ts -H 127.0.0.1 -p 4416 >> "$POT_LOG" 2>&1 &
        POT_PID=$!
        export BGUTIL_PID="$POT_PID"
        export BGUTIL_CMD="deno run --allow-env --allow-net --allow-ffi --allow-read $POT_DIR/src/main.ts -H 127.0.0.1 -p 4416"
        cd /app
    elif command -v node >/dev/null 2>&1 && [ -f "$POT_DIR/src/main.ts" ]; then
        echo "[CONTAINER] Runtime: Node.js (via ts-node/tsx if available)"
        cd "$POT_DIR"
        node build/main.js -H 127.0.0.1 -p 4416 >> "$POT_LOG" 2>&1 &
        POT_PID=$!
        export BGUTIL_PID="$POT_PID"
        export BGUTIL_CMD="node $POT_DIR/build/main.js -H 127.0.0.1 -p 4416"
        cd /app
    fi

    echo "[CONTAINER] Provider spawned with PID $POT_PID. Awaiting /ping readiness on 127.0.0.1:4416..."
    READY=0
    for i in $(seq 1 40); do
        if curl -sf http://127.0.0.1:4416/ping >/dev/null 2>&1; then
            echo "[CONTAINER] POT Provider is healthy and responding on 127.0.0.1:4416! (attempt $i)"
            READY=1
            break
        fi
        sleep 0.5
    done

    if [ "$READY" -ne 1 ]; then
        echo "[CONTAINER WARNING] POT Provider readiness check timed out. Diagnostic log tail:"
        tail -n 30 "$POT_LOG" 2>/dev/null || true
    fi
else
    echo "[CONTAINER] No pot-provider distribution detected in /opt/pot-provider or /app/pot-provider."
fi

# Start FastAPI Uvicorn engine
APP_PORT="${PORT:-8000}"
echo "[CONTAINER] Starting FastAPI Uvicorn engine on 0.0.0.0:${APP_PORT}..."
exec uvicorn app:app --host 0.0.0.0 --port "$APP_PORT"
