#!/usr/bin/env python3
"""계단식 부하(steps.js) 결과를 층별 응답시간 표로 만든다. 표준 라이브러리만 쓴다.

입력
  --nginx      nginx perf 로그 (JSON 줄. JSON이 아닌 줄은 무시)
  --k6         steps.js가 남긴 요약 JSON (선택). 클라이언트가 본 시간과 비교할 때 쓴다.
  --resources  collect.py가 남긴 자원 샘플 (선택). 활용 법칙으로 요청당 CPU를 역산할 때 쓴다.

요청 하나의 시간은 아래처럼 겹겹이 들어 있다(바깥 = 안쪽의 합 + 자기 몫).
  client(k6)  = 네트워크·k6 몫 + nginx rt
  nginx rt    = verify(vt) + upstream(urt) + nginx 자체
  vt          = verify 앱 시간 + verify 대기(네트워크·uvicorn·이벤트 루프)
  urt         = 앱 시간 + upstream 대기(네트워크·uvicorn·이벤트 루프)
  앱 시간     = redis + db(풀 대기 + 쿼리) + hash_wait + hash + 나머지(파이썬 CPU 등)
평균은 더해지지만 p95는 더해지지 않는다. 그래서 분해표는 평균으로, p95는 따로 보여 준다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict

OPS = ["read", "read_next", "write", "login"]
# nginx 시간은 1ms 단위라, 층 사이 비교는 반올림 오차를 이만큼 허용한다.
ROUNDING_MS = 1.5

COMPONENTS = [
    ("net_client", "네트워크·k6", "k6 평균 - nginx rt 평균 (k6 요약이 있을 때만)"),
    ("nginx_self", "nginx 자체", "rt - vt - urt"),
    ("verify_wait", "verify 대기", "vt - verify 앱 시간"),
    ("verify_app", "verify 앱", "auth-verify 안에서 쓴 시간"),
    ("upstream_wait", "앱 앞 대기", "urt - 앱 시간 (네트워크·uvicorn·이벤트 루프 대기)"),
    ("redis", "redis", "Redis 왕복 합계"),
    ("db_pool", "db 풀·연결", "db - db_query (커넥션 대기·pre-ping·커밋)"),
    ("db_query", "db 쿼리", "SQL 실행"),
    ("hash_wait", "argon2 대기", "argon2 슬롯(세마포어) 대기"),
    ("hash", "argon2 계산", "argon2 해시 계산(스레드풀 포함)"),
    ("app_other", "앱 나머지", "앱 시간 - 위 구간들 (파이썬 CPU: 검증·직렬화 등)"),
]


def parse_server_timing(value: str) -> dict[str, float]:
    spans: dict[str, float] = {}
    if not value:
        return spans
    for part in value.split(","):
        fields = [f.strip() for f in part.split(";")]
        attrs = dict(f.split("=", 1) for f in fields[1:] if "=" in f)
        if "dur" in attrs:
            spans[fields[0]] = float(attrs["dur"])
    return spans


def seconds_field(value) -> float | None:
    """nginx upstream 시간 필드. 빈 값/'-'는 None, 재시도로 여러 값이면 합친다."""
    if value in ("", "-", None):
        return None
    total = 0.0
    for piece in str(value).replace(":", ",").split(","):
        piece = piece.strip()
        if piece and piece != "-":
            total += float(piece)
    return total


def decompose(line: dict) -> dict[str, float] | None:
    rt = line["rt"] * 1000
    urt = seconds_field(line.get("urt"))
    if urt is None:
        return None
    urt *= 1000
    vt = (seconds_field(line.get("vt")) or 0.0) * 1000
    app = parse_server_timing(line.get("st_app", ""))
    ver = parse_server_timing(line.get("st_verify", ""))
    if "app" not in app:
        return None

    db = app.get("db", 0.0)
    db_query = app.get("db_query", 0.0)
    measured = app.get("redis", 0.0) + db + app.get("hash_wait", 0.0) + app.get("hash", 0.0)
    return {
        "rt": rt,
        "vt": vt,
        "urt": urt,
        "app": app["app"],
        "nginx_self": rt - vt - urt,
        "verify_wait": vt - ver["app"] if "app" in ver else 0.0,
        "verify_app": ver.get("app", 0.0),
        "upstream_wait": urt - app["app"],
        "redis": app.get("redis", 0.0),
        "db_pool": db - db_query,
        "db_query": db_query,
        "hash_wait": app.get("hash_wait", 0.0),
        "hash": app.get("hash", 0.0),
        "app_other": app["app"] - measured,
        # 층 순서가 깨졌는지(계측 오류 신호)
        "violation": float(
            app["app"] > urt + ROUNDING_MS
            or ("app" in ver and ver["app"] > vt + ROUNDING_MS)
            or vt + urt > rt + ROUNDING_MS
            or measured > app["app"] + 0.01
        ),
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[k]


def main_cluster(times: list[float], gap: float = 10.0) -> tuple[float, float]:
    """단계 하나의 요청 시각 중 가장 큰 연속 구간. 다른 실행에서 섞여 든 요청(같은 단계 태그)을 걸러 낸다."""
    times = sorted(times)
    best = (times[0], times[0], 1)
    start, count = times[0], 1
    for prev, cur in zip(times, times[1:]):
        if cur - prev > gap:
            start, count = cur, 0
        count += 1
        if count > best[2]:
            best = (start, cur, count)
    return best[0], best[1]


def load_nginx(path: str):
    lines: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            start = raw.find("{")
            if start < 0:
                continue
            try:
                line = json.loads(raw[start:])
            except json.JSONDecodeError:
                continue
            if line.get("step") and line.get("op") in OPS:
                lines.append(line)

    by_step: dict[int, list[float]] = defaultdict(list)
    for line in lines:
        by_step[int(line["step"])].append(float(line["ts"]))
    window = {step: list(main_cluster(times)) for step, times in by_step.items()}

    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    errors: dict[tuple[int, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    dropped = 0
    for line in lines:
        key = (int(line["step"]), line["op"])
        lo, hi = window[key[0]]
        if not lo <= float(line["ts"]) <= hi:
            dropped += 1
            continue
        if line["status"] >= 400:
            errors[key][line["status"]] += 1
            continue
        parts = decompose(line)
        if parts is not None:
            groups[key].append(parts)
    if dropped:
        print(f"참고: 단계 구간 밖 요청 {dropped}건을 제외했습니다(다른 실행에서 섞인 요청).", file=sys.stderr)
    return groups, errors, window


def load_k6(path: str | None) -> dict[tuple[int, str], dict]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        metrics = json.load(f)["metrics"]
    result: dict[tuple[int, str], dict] = {}
    for name, body in metrics.items():
        if not name.startswith("http_req_duration{"):
            continue
        tags = dict(t.split(":", 1) for t in name[name.index("{") + 1 : -1].split(","))
        values = body.get("values", {})
        if "step" not in tags or "op" not in tags or not values.get("count"):
            continue
        result[(int(tags["step"]), tags["op"])] = values
    return result


def deployment_of(pod: str) -> str:
    # auth-verify-6d9f7c9b8-x2k4p -> auth-verify
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) > 2 else pod


def load_resources(path: str | None, window: dict[int, list[float]]):
    """단계별로 Deployment마다 평균 CPU(코어), 평균 Pod 수, 이벤트 루프 지연, 노드 메모리 지표를 낸다."""
    if not path:
        return {}, {}
    top: dict[int, dict[str, dict[float, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    lag: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    node: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                s = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if s["kind"] == "top":
                for step, (lo, hi) in window.items():
                    if lo <= s["ts"] <= hi:
                        top[step][deployment_of(s["pod"])][s["ts"]].append(s["cpu_m"] / 1000)
                        top[step][deployment_of(s["pod"]) + "#mem"][s["ts"]].append(s["mem_mi"])
            elif s["kind"] == "lag":
                lag[s["pod"]].append((s["ts"], s["sum"], s["count"]))
            elif s["kind"] == "node":
                node.append(s)

    per_step: dict[int, dict[str, dict]] = {}
    for step, deps in top.items():
        out: dict[str, dict] = {}
        for dep, samples in deps.items():
            if dep.endswith("#mem"):
                continue
            out[dep] = {
                "cpu": statistics.mean(sum(v) for v in samples.values()),
                "pods": statistics.mean(len(v) for v in samples.values()),
                "mem_mi": statistics.mean(sum(v) for v in deps[dep + "#mem"].values()),
            }
        per_step[step] = out

    # 이벤트 루프 지연: 단계 창 안에서 누적 sum/count 증가분으로 평균을 낸다.
    for step, (lo, hi) in window.items():
        by_dep: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for pod, samples in lag.items():
            inside = [x for x in samples if lo <= x[0] <= hi]
            if len(inside) >= 2 and inside[-1][2] >= inside[0][2]:
                by_dep[deployment_of(pod)][0] += inside[-1][1] - inside[0][1]
                by_dep[deployment_of(pod)][1] += inside[-1][2] - inside[0][2]
        for dep, (s, c) in by_dep.items():
            if c:
                per_step.setdefault(step, {}).setdefault(dep, {})["lag_ms"] = s / c * 1000

    node_per_step: dict[int, dict] = {}
    for step, (lo, hi) in window.items():
        inside = [n for n in node if lo <= n["ts"] <= hi]
        if len(inside) >= 2:
            first, last = inside[0], inside[-1]
            dt = last["ts"] - first["ts"] or 1
            node_per_step[step] = {
                "mem_psi_avg10": statistics.mean(n["mem_psi_some_avg10"] for n in inside),
                "swap_in_per_s": (last["pswpin"] - first["pswpin"]) / dt,
                "major_faults_per_s": (last["pgmajfault"] - first["pgmajfault"]) / dt,
                "mem_available_mi": min(n["mem_available_mi"] for n in inside),
            }
    return per_step, node_per_step


def fmt(v: float) -> str:
    return "-" if v != v else f"{v:.1f}"  # NaN -> "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nginx", required=True)
    ap.add_argument("--k6")
    ap.add_argument("--resources")
    args = ap.parse_args()

    groups, errors, window = load_nginx(args.nginx)
    k6 = load_k6(args.k6)
    resources, node = load_resources(args.resources, window)
    if not groups and not errors:
        print("step/op가 붙은 요청이 로그에 없습니다. steps.js로 만든 부하인지, 로그 범위가 맞는지 확인하세요.")
        return 1

    steps = sorted({k[0] for k in list(groups) + list(errors)})
    out: list[str] = []
    w = out.append

    w("## 1. 단계별 요약\n")
    w("| 단계(목표 rps) | op | 성공 수 | 실제 rps | 에러(상태:수) | k6 평균 | k6 p95 | nginx 평균 | nginx p95 | 층 순서 위반 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for step in steps:
        lo, hi = window.get(step, [0, 0])
        dur = max(hi - lo, 1)
        for op in OPS:
            rows = groups.get((step, op), [])
            errs = errors.get((step, op), {})
            if not rows and not errs:
                continue
            client = k6.get((step, op), {})
            rt = [r["rt"] for r in rows]
            err_text = ", ".join(f"{s}:{n}" for s, n in sorted(errs.items())) or "-"
            viol = sum(r["violation"] for r in rows)
            w(
                f"| {step} | {op} | {len(rows)} | {(len(rows) + sum(errs.values())) / dur:.1f} | {err_text} "
                f"| {fmt(client.get('avg', float('nan')))} | {fmt(client.get('p(95)', float('nan')))} "
                f"| {fmt(statistics.mean(rt) if rt else float('nan'))} | {fmt(percentile(rt, 95))} "
                f"| {int(viol)}/{len(rows)} |"
            )

    w("\n## 2. 평균 응답시간 분해 (ms)\n")
    w("각 칸을 더하면 맨 오른쪽 합계가 됩니다. 합계는 k6 평균이고, k6 요약이 없거나 에러가 섞인 그룹은 성공 요청의 nginx 평균입니다.\n")
    w("| 단계 | op | " + " | ".join(label for _, label, _ in COMPONENTS) + " | 합계 |")
    w("|---|---|" + "---|" * (len(COMPONENTS) + 1))
    for step in steps:
        for op in OPS:
            rows = groups.get((step, op), [])
            if not rows:
                continue
            means = {key: statistics.mean(r[key] for r in rows if key in r) for key, _, _ in COMPONENTS[1:]}
            rt_mean = statistics.mean(r["rt"] for r in rows)
            client = k6.get((step, op), {})
            # k6 평균은 에러 응답까지 포함하므로, 에러가 섞인 그룹에서는 뺄셈이 의미가 없다.
            clean = not errors.get((step, op))
            means["net_client"] = client["avg"] - rt_mean if "avg" in client and clean else float("nan")
            total = client["avg"] if "avg" in client and clean else rt_mean
            w(f"| {step} | {op} | " + " | ".join(fmt(means[key]) for key, _, _ in COMPONENTS) + f" | {fmt(total)} |")

    w("\n## 3. 구간별 p95 (ms, 더해지지 않음)\n")
    w("| 단계 | op | " + " | ".join(label for _, label, _ in COMPONENTS[1:]) + " |")
    w("|---|---|" + "---|" * (len(COMPONENTS) - 1))
    for step in steps:
        for op in OPS:
            rows = groups.get((step, op), [])
            if rows:
                cells = [fmt(percentile([r[key] for r in rows], 95)) for key, _, _ in COMPONENTS[1:]]
                w(f"| {step} | {op} | " + " | ".join(cells) + " |")

    if resources:
        w("\n## 4. 자원과 활용 법칙 (요청당 CPU = 사용 코어 / 처리량)\n")
        w("| 단계 | Deployment | 평균 Pod 수 | 사용 CPU(코어) | 메모리(MiB) | 이벤트 루프 지연 평균(ms) | 요청당 CPU(ms) |")
        w("|---|---|---|---|---|---|---|")
        # Deployment마다 그 Deployment를 거치는 요청 수(단계 전체)
        for step in steps:
            lo, hi = window.get(step, [0, 0])
            dur = max(hi - lo, 1)
            count = {op: len(groups.get((step, op), [])) + sum(errors.get((step, op), {}).values()) for op in OPS}
            through = {
                "board-api": count["read"] + count["read_next"] + count["write"],
                "auth-verify": count["read"] + count["read_next"] + count["write"],
                "auth": count["login"],
                "nginx": sum(count.values()),
            }
            for dep, r in sorted(resources.get(step, {}).items()):
                if "cpu" not in r:
                    continue
                n = through.get(dep)
                per_req = r["cpu"] / (n / dur) * 1000 if n else float("nan")
                w(
                    f"| {step} | {dep} | {r['pods']:.1f} | {r['cpu']:.2f} | {r['mem_mi']:.0f} "
                    f"| {fmt(r.get('lag_ms', float('nan')))} | {fmt(per_req)} |"
                )

    if node:
        w("\n## 5. 노드 메모리\n")
        w("| 단계 | 메모리 압박(PSI some avg10, %) | swap-in(쪽/초) | major fault(회/초) | 최소 가용 메모리(MiB) |")
        w("|---|---|---|---|---|")
        for step in steps:
            if step in node:
                n = node[step]
                w(
                    f"| {step} | {n['mem_psi_avg10']:.2f} | {n['swap_in_per_s']:.1f} "
                    f"| {n['major_faults_per_s']:.1f} | {n['mem_available_mi']:.0f} |"
                )

    w("\n## 용어\n")
    for _, label, desc in COMPONENTS:
        w(f"- **{label}**: {desc}")
    w("- **층 순서 위반**: 안쪽 층 시간이 바깥 층보다 긴 요청 수. 0이 아니면 계측이나 로그 파싱을 의심합니다.")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
