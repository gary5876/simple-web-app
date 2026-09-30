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
