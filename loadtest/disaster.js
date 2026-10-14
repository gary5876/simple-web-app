// 재난 상황 트래픽 시뮬레이션 v2 (spec §11)
// 토스증권 미국 정규장 개장 모양을 본떴다. 참고: https://toss.tech/article/28738
//   개장 직후 주문 호출이 평소의 20배 이상으로 급증하고 최소 2시간 이어진다.
//   개장 직후 쌓아 둔 예약 주문(100만 건 이상)을 한꺼번에 보낸다.
// 장전 → 10초 만에 20배 → 개장 초반 유지 → 개장 지속 → 진정. 개장 순간에 예약 쓰기를 한꺼번에 넣는다.
// v1(평상시 → 30초 만에 20배 → 5분 유지 → 감소 → 두 번째 폭증)은 git 태그 loadtest-disaster-v1에 있다.
//
// 환경 변수 (모두 선택):
//   USERS     setup에서 가입하고 로그인할 사용자 수 (기본 500, QUICK 20, local 60)
//   BASELINE  장전 초당 요청 수 (기본 20, QUICK 5, local 4)
//   PEAK      개장 후 초당 요청 수 (기본 BASELINE x 20)
//   MIX       읽기,쓰기,로그인 비율(%) (기본 80,15,5)
//   PRE, RAMP, OPEN, HOLD, COOL  구간 길이 (기본 2m, 10s, 5m, 10m, 2m)
//             HOLD는 2시간을 재현하는 구간이 아니라 안정 상태에 들어가는지 보는 구간이다. 장시간 테스트는 HOLD=2h
//   RESERVED  개장 순간 사용자마다 넣는 예약 쓰기 수 (기본 4)
//   OUT       결과 폴더. 주면 summary.json과 meta.json을 남긴다(Make 타깃이 버전별 폴더를 만들어 넘긴다)
//   QUICK=1   구간마다 10초로 줄인다. PROFILE=local 노트북용 축소 기본값
import exec from 'k6/execution';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.1.0/index.js';

import { BASE_URL, createUsers, login, read, write } from './lib.js';

// 시나리오 모양을 바꾸면 이 값을 올린다. Makefile이 이 줄을 읽어 결과 폴더 이름으로 쓴다.
export const SCENARIO_ID = 'disaster-v2';

const QUICK = __ENV.QUICK === '1';
const LOCAL = __ENV.PROFILE === 'local';
const USERS = parseInt(__ENV.USERS || (QUICK ? '20' : LOCAL ? '60' : '500'), 10);
const BASELINE = parseInt(__ENV.BASELINE || (QUICK ? '5' : LOCAL ? '4' : '20'), 10);
const PEAK = parseInt(__ENV.PEAK || String(BASELINE * 20), 10);
const [READ_PCT, WRITE_PCT] = (__ENV.MIX || '80,15,5').split(',').map((v) => parseInt(v, 10));
// nginx는 세션마다 글쓰기를 순간 5건까지 받는다. 하나 남겨 두어 같은 순간 실시간 쓰기와 겹쳐도 429가 나지 않게 한다.
const RESERVED = parseInt(__ENV.RESERVED || '4', 10);

const length = (name, full) => (QUICK ? '10s' : __ENV[name] || full);
const PHASES = [
  { name: 'pre', duration: length('PRE', '2m'), target: BASELINE }, // 장전
  { name: 'surge', duration: length('RAMP', '10s'), target: PEAK }, // 개장 급증
  { name: 'open', duration: length('OPEN', '5m'), target: PEAK }, // 개장 초반
  { name: 'hold', duration: length('HOLD', '10m'), target: PEAK }, // 개장 지속 (안정 상태 확인)
  { name: 'cool', duration: length('COOL', '2m'), target: BASELINE }, // 진정
];

// '1h30m', '10s' 같은 길이를 초로 바꾼다.
function seconds(text) {
  const units = { h: 3600, m: 60, s: 1 };
  let total = 0;
  for (const [, n, unit] of text.matchAll(/(\d+)([hms])/g)) total += parseInt(n, 10) * units[unit];
  return total;
}

// 구간이 끝나는 시각(초, 시나리오 시작 기준)
const PHASE_ENDS = PHASES.reduce((ends, p) => [...ends, (ends.at(-1) || 0) + seconds(p.duration)], []);
const OPEN_AT = PHASE_ENDS[0];

function phaseNow() {
  const elapsed = (Date.now() - exec.scenario.startTime) / 1000;
  const i = PHASE_ENDS.findIndex((end) => elapsed < end);
  return PHASES[i < 0 ? PHASES.length - 1 : i].name;
}

// 구간마다 항상 통과하는 임계치를 걸어 요약에 구간별 수치가 나오게 한다(steps.js와 같은 방법).
function phaseThresholds() {
  const result = {};
  for (const phase of [...PHASES.map((p) => p.name), 'reserved']) {
    result[`write_accepted_duration{phase:${phase}}`] = ['max>=0'];
    result[`server_errors{phase:${phase}}`] = ['rate>=0'];
    if (phase !== 'reserved') result[`read_duration{phase:${phase}}`] = ['max>=0'];
  }
  return result;
}

export const options = {
  setupTimeout: '10m',
  tags: { test: SCENARIO_ID },
  scenarios: {
    live: {
      executor: 'ramping-arrival-rate',
      startRate: BASELINE,
      timeUnit: '1s',
      preAllocatedVUs: Math.max(50, PEAK),
      maxVUs: PEAK * 5,
      stages: PHASES.map((p) => ({ target: p.target, duration: p.duration })),
    },
    // 예약 주문 일괄 전송에 해당한다. 개장 순간 사용자마다 RESERVED건씩 최대한 빨리 넣는다.
    reserved: {
      executor: 'shared-iterations',
      exec: 'reserved',
      startTime: `${OPEN_AT}s`,
      vus: Math.min(USERS, 100),
      iterations: USERS * RESERVED,
      maxDuration: PHASES[2].duration,
    },
  },
  thresholds: {
    read_duration: ['p(95)<300'],
    write_accepted_duration: ['p(95)<100'],
    server_errors: ['rate<0.01'],
    ...phaseThresholds(),
  },
};

export function setup() {
  return { users: createUsers(USERS) };
}

export default function (data) {
  exec.vu.metrics.tags.phase = phaseNow();
  // 반복 번호로 사용자를 고르게 돌려 써서, 세션 쿠키(sid)별 글쓰기 리밋(2r/s)에 걸리지 않게 한다.
  // 예: PEAK 400r/s x 쓰기 15% / 500명 = 사용자당 0.12r/s
  const user = data.users[exec.scenario.iterationInTest % data.users.length];
  const r = Math.random() * 100;
  if (r < READ_PCT) read();
  else if (r < READ_PCT + WRITE_PCT) write(user);
  else login(user);
}

export function reserved(data) {
  exec.vu.metrics.tags.phase = 'reserved';
  write(data.users[exec.scenario.iterationInTest % data.users.length]);
}

export function handleSummary(data) {
  const out = { stdout: textSummary(data, { indent: ' ', enableColors: true }) };
  if (__ENV.OUT) {
    const meta = {
      scenario: SCENARIO_ID,
      finished_at: new Date().toISOString(),
      base_url: BASE_URL,
      mode: QUICK ? 'quick' : LOCAL ? 'local' : 'full',
      users: USERS,
      baseline: BASELINE,
      peak: PEAK,
      mix: __ENV.MIX || '80,15,5',
      reserved_per_user: RESERVED,
      phases: PHASES.map((p) => ({ name: p.name, duration: p.duration, target: p.target })),
    };
    out[`${__ENV.OUT}/summary.json`] = JSON.stringify(data, null, 2);
    out[`${__ENV.OUT}/meta.json`] = JSON.stringify(meta, null, 2);
    out.stdout += `\n결과 저장: ${__ENV.OUT}\n`;
  }
  return out;
}
