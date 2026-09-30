#!/usr/bin/env bash
# 사용법: scripts/wait-http.sh <url> [timeout_seconds]
set -euo pipefail
url="$1"
timeout="${2:-120}"
deadline=$((SECONDS + timeout))
until curl -sf -o /dev/null "$url"; do
  if (( SECONDS >= deadline )); then
    echo "timed out waiting for $url" >&2
    exit 1
  fi
  sleep 1
done
