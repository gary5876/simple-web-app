#!/usr/bin/env python3
"""부하 테스트 동안 자원 사용을 주기적으로 기록한다(JSON 줄). analyze.py --resources 로 넘긴다.

기록하는 것
  top   Pod별 CPU(밀리코어)·메모리(MiB)       ← kubectl top pods (metrics-server 필요)
  lag   Pod별 이벤트 루프 지연 누적 sum/count ← 앱 /metrics 의 event_loop_lag_seconds
  node  노드 메모리 압박·스왑·major fault     ← 이 스크립트를 돌리는 머신의 /proc (Linux일 때만)

kind는 노드가 호스트 커널을 같이 쓰므로, kind를 띄운 VM에서 돌리면 node 값이 곧 클러스터 노드 값이다.
Ctrl+C로 멈춘다. 예: python3 loadtest/collect.py > loadtest/results/resources.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# /metrics는 nginx가 밖으로 막아 두었으므로 Pod 안에서 직접 읽는다.
METRICS_SNIPPET = (
    "import urllib.request as u;"
    "print(u.urlopen('http://127.0.0.1:8000/metrics', timeout=3).read().decode())"
)
LAG_APPS = ("auth", "auth-verify", "board-api")


def kubectl(ns: str, *args: str, timeout: float = 20) -> str:
    return subprocess.run(
        ["kubectl", "-n", ns, *args], capture_output=True, text=True, timeout=timeout, check=True
    ).stdout


def emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def sample_top(ns: str, ts: float) -> None:
    try:
        out = kubectl(ns, "top", "pods", "--no-headers")
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"top 실패: {exc}", file=sys.stderr)
        return
    for line in out.splitlines():
        name, cpu, mem = line.split()[:3]
        emit({"kind": "top", "ts": ts, "pod": name, "cpu_m": int(cpu.rstrip("m")), "mem_mi": int(mem.rstrip("Mi"))})


def lag_of(ns: str, pod: str) -> tuple[float, float] | None:
    try:
        out = kubectl(ns, "exec", pod, "--", "python", "-c", METRICS_SNIPPET, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    total = count = None
    for line in out.splitlines():
        if line.startswith("event_loop_lag_seconds_sum"):
            total = float(line.split()[-1])
        elif line.startswith("event_loop_lag_seconds_count"):
            count = float(line.split()[-1])
    return (total, count) if total is not None and count is not None else None


def sample_lag(ns: str, ts: float, pool: ThreadPoolExecutor) -> None:
    selector = "app.kubernetes.io/name in (" + ",".join(LAG_APPS) + ")"
    try:
        pods = kubectl(ns, "get", "pods", "-l", selector, "--field-selector=status.phase=Running", "-o", "name").split()
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"pod 목록 실패: {exc}", file=sys.stderr)
        return
    names = [p.removeprefix("pod/") for p in pods]
    for pod, result in zip(names, pool.map(lambda p: lag_of(ns, p), names)):
        if result is not None:
            emit({"kind": "lag", "ts": ts, "pod": pod, "sum": result[0], "count": result[1]})


def sample_node(ts: float) -> None:
    if not os.path.exists("/proc/pressure/memory"):
        return
    with open("/proc/pressure/memory") as f:
        some = f.readline().split()  # some avg10=0.00 avg60=0.00 avg300=0.00 total=0
    vmstat = {}
    with open("/proc/vmstat") as f:
        for line in f:
            key, value = line.split()
            vmstat[key] = int(value)
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":")
            meminfo[key] = int(value.split()[0])
    emit(
        {
            "kind": "node",
            "ts": ts,
            "mem_psi_some_avg10": float(some[1].split("=")[1]),
            "pswpin": vmstat.get("pswpin", 0),
            "pgmajfault": vmstat.get("pgmajfault", 0),
            "mem_available_mi": meminfo["MemAvailable"] // 1024,
        }
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--namespace", default="simple-web-app")
    ap.add_argument("--interval", type=float, default=10.0, help="샘플 간격(초). metrics-server 갱신 주기(약 15초)보다 짧아도 된다")
    args = ap.parse_args()

    with ThreadPoolExecutor(max_workers=8) as pool:
        try:
            while True:
                started = time.time()
                sample_node(started)
                sample_top(args.namespace, started)
                sample_lag(args.namespace, started, pool)
                time.sleep(max(0.0, args.interval - (time.time() - started)))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
