#!/usr/bin/env bash
# End-to-end check against the real bus data server. Needs the API key configured for the server
# (see the server README) and a Docker host; the first run builds the Kotlin server (several minutes).
set -euo pipefail

URL="${ANALYTICS_URL:-http://127.0.0.1:8081}"
ROUTE="${SMOKE_ROUTE:-15}"
MAX_WAIT="${SMOKE_MAX_WAIT:-600}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

cd "$(dirname "$0")/../../dublin-bus-server"
docker compose up -d --build

echo "waiting for $URL/api/health"
for _ in $(seq 60); do
  curl -fsS "$URL/api/health" >/dev/null 2>&1 && break
  sleep 5
done
curl -fsS "$URL/api/health"; echo

curl -fsS -X PUT "$URL/api/routes/tracked/$ROUTE"; echo
echo "tracking route $ROUTE; sleeping 90 s for the first polls"
sleep 90

since=$(( $(date +%s) - 86400 ))
start=$(date +%s)
while :; do
  code=$(curl -s -o "$OUT" -w '%{http_code}' "$URL/api/routes/$ROUTE/summary?since=$since" || true)
  echo "summary: HTTP $code, $(( $(date +%s) - start + 90 )) s after tracking"
  if [ "$code" = 200 ] && grep -Eq '"n": ?[1-9]' "$OUT"; then break; fi
  if [ $(( $(date +%s) - start )) -ge "$MAX_WAIT" ]; then
    echo "FAIL: no arrivals recorded within ${MAX_WAIT} s"; exit 1
  fi
  sleep 30
done
curl -fsS "$URL/api/health"; echo
echo "OK. Stop with: (cd $(pwd) && docker compose down)"
