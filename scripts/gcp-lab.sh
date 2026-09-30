#!/usr/bin/env bash
# GCP에 부하 테스트 실험실을 띄운다: kind를 돌릴 VM(lab) + k6를 돌릴 VM(k6). 절차는 loadtest/README.md 참고.
#
#   scripts/gcp-lab.sh up       VM 두 대 생성 (도구 설치는 부팅 스크립트가 백그라운드로 함, 약 3분)
#   scripts/gcp-lab.sh push     현재 커밋(HEAD)을 두 VM의 ~/simple-web-app 에 올림 (git push 불필요)
#   scripts/gcp-lab.sh ssh      lab VM 접속     / ssh-k6: k6 VM 접속
#   scripts/gcp-lab.sh status   VM 상태와 lab 내부 IP (k6에서 BASE_URL=http://<내부IP>:8080)
#   scripts/gcp-lab.sh down     VM과 디스크 삭제
#
# 필수 환경 변수: GCP_PROJECT. 선택: GCP_ACCOUNT, GCP_ZONE(기본 asia-northeast3-a),
#                 LAB_MACHINE(기본 n2-standard-8), K6_MACHINE(기본 e2-standard-2)
set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT를 지정하세요}"
ZONE=${GCP_ZONE:-asia-northeast3-a}
LAB=${LAB_NAME:-loadtest-lab}
K6=${K6_NAME:-loadtest-k6}
LAB_MACHINE=${LAB_MACHINE:-n2-standard-8}
K6_MACHINE=${K6_MACHINE:-e2-standard-2}
KIND_VERSION=v0.33.0
KUBECTL_VERSION=v1.34.1

gc() {
  gcloud --project "$GCP_PROJECT" ${GCP_ACCOUNT:+--account "$GCP_ACCOUNT"} "$@"
}

# 부팅 스크립트. 끝나면 /var/log/lab-ready 가 생긴다.
startup_script() {
  local role=$1
  cat <<EOF
#!/bin/bash
set -eux
[ -f /var/log/lab-ready ] && exit 0
export DEBIAN_FRONTEND=noninteractive HOME=/root
apt-get update
apt-get install -y make python3 git jq ca-certificates curl gnupg

# k6 (공식 apt 저장소)
gpg -k >/dev/null || true
gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \\
  --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" > /etc/apt/sources.list.d/k6.list
apt-get update
apt-get install -y k6

if [ "$role" = lab ]; then
  apt-get install -y docker.io docker-buildx
  curl -fsSLo /usr/local/bin/kind https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-amd64
  curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl
  chmod +x /usr/local/bin/kind /usr/local/bin/kubectl
  # kind에 Pod가 많으면 inotify 한도에 걸린다(kind 알려진 문제).
  sysctl -w fs.inotify.max_user_watches=524288 fs.inotify.max_user_instances=512
  # 메모리 실험용 스왑. 평소엔 32GB라 쓰이지 않고, kind 노드 메모리를 제한했을 때만 쓰인다.
  fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
fi
touch /var/log/lab-ready
EOF
}

create_vm() {
  local name=$1 machine=$2 disk=$3 role=$4
  local script
  script=$(mktemp)
  startup_script "$role" > "$script"
  gc compute instances create "$name" --zone "$ZONE" --machine-type "$machine" \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size "${disk}GB" --boot-disk-type pd-balanced \
    --metadata-from-file startup-script="$script" --labels purpose=loadtest
  rm -f "$script"
}

push() {
  local archive
  archive=$(mktemp -t simple-web-app).tar.gz
  git archive --format=tar.gz -o "$archive" HEAD
  for vm in "$LAB" "$K6"; do
    gc compute scp --zone "$ZONE" "$archive" "$vm:~/simple-web-app.tar.gz"
    # docker 그룹은 다음 접속부터 적용된다.
    gc compute ssh "$vm" --zone "$ZONE" --command \
      'rm -rf ~/simple-web-app && mkdir ~/simple-web-app && tar -xzf ~/simple-web-app.tar.gz -C ~/simple-web-app \
       && (getent group docker >/dev/null && sudo usermod -aG docker "$USER" || true) \
       && (test -f /var/log/lab-ready && echo "도구 설치 완료" || echo "아직 도구 설치 중: sudo tail -f /var/log/syslog | grep startup")'
  done
  rm -f "$archive"
  echo "올린 커밋: $(git rev-parse --short HEAD)"
}

case "${1:-}" in
  up)
    create_vm "$LAB" "$LAB_MACHINE" 100 lab
    create_vm "$K6" "$K6_MACHINE" 20 k6
    echo "부팅 스크립트가 도구를 설치하는 중입니다(약 3분). 이어서: scripts/gcp-lab.sh push"
    ;;
  push) push ;;
  ssh) shift; gc compute ssh "$LAB" --zone "$ZONE" "$@" ;;
  ssh-k6) shift; gc compute ssh "$K6" --zone "$ZONE" "$@" ;;
  status)
    gc compute instances list --filter="labels.purpose=loadtest" \
      --format="table(name,status,machineType.basename(),networkInterfaces[0].networkIP)"
    ;;
  down)
    gc compute instances delete "$LAB" "$K6" --zone "$ZONE" --quiet
    ;;
  *)
    sed -n '2,11p' "$0"
    exit 1
    ;;
esac
