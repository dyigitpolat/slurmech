#!/usr/bin/env bash
# Run on the GPU server (sura) to verify the xlog1 reverse tunnel is up.
set -euo pipefail

TUNNEL_HOST="${TUNNEL_HOST:-127.0.0.1}"
TUNNEL_PORT="${TUNNEL_PORT:-2222}"
CLUSTER_USER="${CLUSTER_USER:-yigit}"

if ! nc -z "$TUNNEL_HOST" "$TUNNEL_PORT" 2>/dev/null; then
  echo "FAIL: nothing listening on ${TUNNEL_HOST}:${TUNNEL_PORT}"
  echo "Start the bridge on your laptop: scripts/xlog1-bridge-local.sh start"
  exit 1
fi

banner="$(timeout 3 bash -c "echo | nc ${TUNNEL_HOST} ${TUNNEL_PORT}" 2>/dev/null | head -1 || true)"
if [[ "$banner" != SSH-* ]]; then
  echo "FAIL: port open but not SSH (got: ${banner:-empty})"
  exit 1
fi

if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
  -p "$TUNNEL_PORT" "${CLUSTER_USER}@${TUNNEL_HOST}" true 2>/dev/null; then
  echo "OK: ${TUNNEL_HOST}:${TUNNEL_PORT} -> xlog1 (auth succeeded)"
else
  echo "WARN: tunnel up (${banner}) but key auth failed — password auth may still work for slurmech"
fi
