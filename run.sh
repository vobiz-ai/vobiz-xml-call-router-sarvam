#!/usr/bin/env bash
# Start the router, reclaiming the port first.
#
# Without the kill, a second `python app.py` fails to bind, exits, and leaves
# the OLD process serving — so code changes appear to have no effect. That
# wastes an afternoon exactly once.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8090)}"
PORT="${PORT:-8090}"

if pid=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null); then
  echo "reclaiming port ${PORT} from pid ${pid}"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    lsof -ti "tcp:${PORT}" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.2
  done
fi

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
# -u keeps stdout unbuffered. Buffered, the access log lags behind the
# call you are debugging, and `tail -f server.log` shows nothing at the
# moment you most need it.
exec "$PY" -u app.py
