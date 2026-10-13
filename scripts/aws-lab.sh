#!/usr/bin/env bash
# AWS에 부하 테스트 실험실을 띄운다: kind를 돌릴 서버(lab) + k6를 돌릴 서버(k6). gcp-lab.sh의 AWS판이고 절차는 loadtest/README.md 참고.
#
#   scripts/aws-lab.sh up       접속용 키, 보안 그룹, 서버 두 대 생성 (도구 설치는 부팅 스크립트가 백그라운드로 함, 약 3분)
#   scripts/aws-lab.sh push     현재 커밋(HEAD)을 두 서버의 ~/simple-web-app 에 올림 (git push 불필요)
#   scripts/aws-lab.sh ssh      lab 서버 접속     / ssh-k6: k6 서버 접속
#   scripts/aws-lab.sh status   서버 상태와 lab 내부 IP (k6에서 BASE_URL=http://<내부IP>:8080)
#   scripts/aws-lab.sh down     서버, 보안 그룹, 키 삭제
#
# 선택 환경 변수: AWS_REGION(기본 ap-northeast-2), LAB_MACHINE(기본 c7i.2xlarge), K6_MACHINE(기본 c7i.large),
#                 LAB_TTL_MIN(기본 180. 이 시간이 지나면 서버가 스스로 꺼지고, 꺼지면 삭제된다)
set -euo pipefail

REGION=${AWS_REGION:-ap-northeast-2}
LAB=${LAB_NAME:-loadtest-lab}
K6=${K6_NAME:-loadtest-k6}
LAB_MACHINE=${LAB_MACHINE:-c7i.2xlarge}
K6_MACHINE=${K6_MACHINE:-c7i.large}
TTL_MIN=${LAB_TTL_MIN:-180}
KEY_NAME=${LAB_KEY_NAME:-simple-web-app-loadtest}
KEY_FILE=${LAB_KEY_FILE:-$HOME/.ssh/$KEY_NAME.pem}
SG_NAME=${LAB_SG_NAME:-simple-web-app-loadtest}
SSH_USER=ubuntu
KIND_VERSION=v0.33.0
KUBECTL_VERSION=v1.34.1

ec2() {
  aws ec2 --region "$REGION" "$@"
}

# 부팅 스크립트. 끝나면 /var/log/lab-ready 가 생긴다. gcp-lab.sh와 같은 도구를 깔고, 끄는 것을 잊어도 비용이 계속 나가지 않게 종료를 예약한다.
startup_script() {
  local role=$1
  cat <<EOF
#!/bin/bash
set -eux
[ -f /var/log/lab-ready ] && exit 0
shutdown -h +$TTL_MIN
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
  usermod -aG docker $SSH_USER
  curl -fsSLo /usr/local/bin/kind https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-amd64
  curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl
  chmod +x /usr/local/bin/kind /usr/local/bin/kubectl
  # kind에 Pod가 많으면 inotify 한도에 걸린다(kind 알려진 문제).
  sysctl -w fs.inotify.max_user_watches=524288 fs.inotify.max_user_instances=512
fi
touch /var/log/lab-ready
EOF
}

sg_id() {
  ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v '^None$' || true
}

# 이름으로 살아 있는 서버의 값을 하나 꺼낸다. 예: field loadtest-lab PublicIpAddress
field() {
  ec2 describe-instances \
    --filters "Name=tag:Name,Values=$1" "Name=tag:purpose,Values=loadtest" "Name=instance-state-name,Values=pending,running" \
    --query "Reservations[].Instances[0].$2" --output text
}

ensure_key() {
  if ! ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    mkdir -p "$(dirname "$KEY_FILE")"
    ec2 create-key-pair --key-name "$KEY_NAME" --key-type ed25519 --query KeyMaterial --output text > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
  fi
  [ -f "$KEY_FILE" ] || { echo "키 $KEY_NAME 은 AWS에 있는데 $KEY_FILE 이 없습니다. down 후 다시 up 하세요." >&2; exit 1; }
}

# SSH는 지금 이 PC의 공인 IP에서만, 그 밖의 포트는 실험실 서버끼리만 연다.
ensure_sg() {
  local id my_ip vpc
  id=$(sg_id)
  if [ -z "$id" ]; then
    vpc=$(ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
    id=$(ec2 create-security-group --group-name "$SG_NAME" --description "simple-web-app loadtest lab" \
      --vpc-id "$vpc" --query GroupId --output text)
    my_ip=$(curl -fsS https://checkip.amazonaws.com)
    ec2 authorize-security-group-ingress --group-id "$id" --protocol tcp --port 22 --cidr "$my_ip/32" >/dev/null
    ec2 authorize-security-group-ingress --group-id "$id" --protocol tcp --port 0-65535 --source-group "$id" >/dev/null
  fi
  echo "$id"
}

create_vm() {
  local name=$1 machine=$2 disk=$3 role=$4 sg=$5
  local script ami
  script=$(mktemp)
  startup_script "$role" > "$script"
  ami=$(aws ssm get-parameter --region "$REGION" --query Parameter.Value --output text \
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id)
  ec2 run-instances --image-id "$ami" --instance-type "$machine" --key-name "$KEY_NAME" \
    --security-group-ids "$sg" --user-data "file://$script" \
    --instance-initiated-shutdown-behavior terminate --metadata-options HttpTokens=required \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$disk,VolumeType=gp3,DeleteOnTermination=true}" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$name},{Key=purpose,Value=loadtest}]" \
      "ResourceType=volume,Tags=[{Key=Name,Value=$name},{Key=purpose,Value=loadtest}]" \
    --query 'Instances[0].InstanceId' --output text
  rm -f "$script"
}

remote() {
  local name=$1
  shift
  ssh -i "$KEY_FILE" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KEY_FILE.known_hosts" \
    -o ServerAliveInterval=30 "$SSH_USER@$(field "$name" PublicIpAddress)" "$@"
}

push() {
  local archive
  archive=$(mktemp -t simple-web-app).tar.gz
  git archive --format=tar.gz -o "$archive" HEAD
  for vm in "$LAB" "$K6"; do
    scp -i "$KEY_FILE" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KEY_FILE.known_hosts" \
      "$archive" "$SSH_USER@$(field "$vm" PublicIpAddress):simple-web-app.tar.gz"
    remote "$vm" 'rm -rf ~/simple-web-app && mkdir ~/simple-web-app && tar -xzf ~/simple-web-app.tar.gz -C ~/simple-web-app \
       && (test -f /var/log/lab-ready && echo "도구 설치 완료" || echo "아직 도구 설치 중: sudo tail -f /var/log/cloud-init-output.log")'
  done
  rm -f "$archive"
  echo "올린 커밋: $(git rev-parse --short HEAD)"
}

case "${1:-}" in
  up)
    ensure_key
    sg=$(ensure_sg)
    create_vm "$LAB" "$LAB_MACHINE" 60 lab "$sg"
    create_vm "$K6" "$K6_MACHINE" 20 k6 "$sg"
    echo "부팅 스크립트가 도구를 설치하는 중입니다(약 3분). ${TTL_MIN}분 뒤 서버가 스스로 꺼지고 삭제됩니다. 이어서: scripts/aws-lab.sh push"
    ;;
  push) push ;;
  ssh) shift; remote "$LAB" "$@" ;;
  ssh-k6) shift; remote "$K6" "$@" ;;
  status)
    ec2 describe-instances --filters "Name=tag:purpose,Values=loadtest" \
      "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped" \
      --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,State.Name,InstanceType,PrivateIpAddress,PublicIpAddress]' \
      --output table
    ;;
  down)
    ids=$(ec2 describe-instances --filters "Name=tag:purpose,Values=loadtest" \
      "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[].Instances[].InstanceId' --output text)
    if [ -n "$ids" ]; then
      # shellcheck disable=SC2086
      ec2 terminate-instances --instance-ids $ids >/dev/null
      # shellcheck disable=SC2086
      ec2 wait instance-terminated --instance-ids $ids
    fi
    id=$(sg_id)
    [ -n "$id" ] && ec2 delete-security-group --group-id "$id" >/dev/null
    ec2 delete-key-pair --key-name "$KEY_NAME" >/dev/null
    rm -f "$KEY_FILE" "$KEY_FILE.known_hosts"
    echo "삭제 완료"
    ;;
  *)
    sed -n '2,11p' "$0"
    exit 1
    ;;
esac
