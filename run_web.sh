#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON" ]]; then
    echo "Błąd: nie znaleziono Python 3.11+. Utwórz .venv w katalogu projektu." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'; then
    echo "Błąd: wymagany jest Python 3.11 lub nowszy." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import importlib.util, sys; required=("ccxt","fastapi","uvicorn"); sys.exit(0 if all(importlib.util.find_spec(x) for x in required) else 1)'; then
    echo 'Brak wymaganych pakietów. W aktywnym .venv uruchom: python -m pip install -e ".[api]"' >&2
    exit 1
fi

PORT="${TRADINGAGENT_WEB_PORT:-8000}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "Błąd: TRADINGAGENT_WEB_PORT musi być liczbą od 1 do 65535." >&2
    exit 1
fi
if ! "$PYTHON" - "$PORT" <<'PY'
import socket, sys
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
then
    echo "Błąd: port $PORT jest zajęty. Ustaw TRADINGAGENT_WEB_PORT na inny wolny port." >&2
    exit 1
fi

mkdir -p "$ROOT/data"
exec 9>"$ROOT/data/web.lock"
if ! flock -n 9; then
    echo "Błąd: Trading Agent Web jest już uruchomiony z tego katalogu." >&2
    exit 1
fi

LOG="$ROOT/data/web.log"
echo "Trading Agent Web: http://127.0.0.1:$PORT"
echo "Logi: $LOG"
echo "Zatrzymanie: naciśnij Ctrl+C."
"$PYTHON" -m tradingagent.cli --serve --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
APP_PID=$!
stop_app() {
    kill -INT "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
}
trap stop_app INT TERM HUP
set +e
wait "$APP_PID"
STATUS=$?
set -e
trap - INT TERM HUP
exit "$STATUS"
