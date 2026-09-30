import subprocess

from conftest import ROOT

MARKER = "--- real-ip.inc ---"


def test_real_ip_from_accepts_a_space_separated_cidr_list() -> None:
    # docker-entrypoint.sh는 명령이 nginx일 때만 /docker-entrypoint.d/ 스크립트를 실행한다.
    script = f"/docker-entrypoint.sh nginx -t && echo '{MARKER}' && cat /etc/nginx/conf.d/real-ip.inc"
    res = subprocess.run(
        [
            "docker", "compose", "run", "--rm", "--no-deps",
            "-e", "REAL_IP_FROM=130.211.0.0/22 35.191.0.0/16",
            "--entrypoint", "sh", "nginx", "-c", script,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = res.stdout + res.stderr
    assert res.returncode == 0, output
    assert "test is successful" in output
    inc = res.stdout.split(MARKER, 1)[1]
    assert [line.strip() for line in inc.strip().splitlines()] == [
        "set_real_ip_from 130.211.0.0/22;",
        "set_real_ip_from 35.191.0.0/16;",
    ]


def test_verify_requests_use_a_dedicated_upstream() -> None:
    # /_verify는 VERIFY_UPSTREAM(k8s에서는 auth-verify)으로만 나가고, 기본값은 auth다.
    # nginx -t 출력은 stderr로 보내 stdout에는 설정 파일만 남긴다. 설정이 깨지면 && 때문에 종료 코드가 0이 아니다.
    script = "/docker-entrypoint.sh nginx -t >&2 && cat /etc/nginx/conf.d/default.conf"
    res = subprocess.run(
        [
            "docker", "compose", "run", "--rm", "--no-deps",
            "-e", "VERIFY_UPSTREAM=auth-verify:8000",
            "--entrypoint", "sh", "nginx", "-c", script,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "test is successful" in res.stderr
    conf = res.stdout
    assert "upstream verify_backend {\n    server auth-verify:8000;" in conf
    assert "proxy_pass http://verify_backend/internal/verify;" in conf
