#!/usr/bin/env bash
# kind 등 로컬 클러스터에 배포된 스택을 nginx를 통해 종단 간 확인한다.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
NS="${NS:-simple-web-app}"
SUFFIX="$(date +%s)"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr 'A-Z' 'a-z'; else cat /proc/sys/kernel/random/uuid; fi
}
json=(-H 'Content-Type: application/json')

echo "1) nginx health"
curl -fsS "$BASE_URL/nginx-health" >/dev/null

echo "2) 익명 목록 조회"
curl -fsS "$BASE_URL/api/board/posts" | grep -q '"items"'

echo "3) 회원가입 + 로그인"
curl -fsS "${json[@]}" \
  -d "{\"email\":\"smoke-$SUFFIX@example.com\",\"password\":\"smoke-Passw0rd!\",\"nickname\":\"smoke$SUFFIX\"}" \
  "$BASE_URL/api/auth/signup" >/dev/null
curl -fsS -c "$JAR" "${json[@]}" \
  -d "{\"email\":\"smoke-$SUFFIX@example.com\",\"password\":\"smoke-Passw0rd!\"}" \
  "$BASE_URL/api/auth/login" >/dev/null

echo "4) 글쓰기 → 202"
status=$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" "${json[@]}" \
  -H "Idempotency-Key: $(uuid)" \
  -d "{\"title\":\"smoke $SUFFIX\",\"body\":\"k8s smoke test\"}" \
  "$BASE_URL/api/board/posts")
[ "$status" = "202" ] || { echo "   expected 202, got $status"; exit 1; }

echo "5) 워커가 처리해서 목록에 나타나는지 (최대 15초)"
found=0
for _ in $(seq 1 15); do
  if curl -fsS "$BASE_URL/api/board/posts" | grep -q "smoke $SUFFIX"; then found=1; break; fi
  sleep 1
done
[ "$found" = "1" ] || { echo "   post not visible"; exit 1; }

echo "6) 위조한 X-User-Id 헤더로 익명 글쓰기 → 401"
status=$(curl -sS -o /dev/null -w '%{http_code}' "${json[@]}" \
  -H 'X-User-Id: 00000000-0000-0000-0000-000000000001' \
  -H "Idempotency-Key: $(uuid)" \
  -d '{"title":"spoof","body":"spoof"}' \
  "$BASE_URL/api/board/posts")
[ "$status" = "401" ] || { echo "   expected 401, got $status"; exit 1; }

echo "7) NetworkPolicy: 다른 Pod에서 auth 직접 접근 차단"
if kubectl -n "$NS" run np-probe --rm -i --quiet --restart=Never \
     --image=curlimages/curl:8.10.1 --labels=app.kubernetes.io/name=np-probe \
     -- curl -sS -m 3 http://auth:8000/healthz >/dev/null 2>&1; then
  echo "   WARN: auth에 직접 접근됨 — 이 클러스터의 CNI가 NetworkPolicy를 적용하지 않음"
  [ "${REQUIRE_NETPOL:-0}" = "1" ] && exit 1
else
  echo "   차단됨"
fi

echo "SMOKE OK"
