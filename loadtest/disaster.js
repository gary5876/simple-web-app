// 재난 상황 트래픽 시뮬레이션 (spec §11)
// 평상시 → 30초 만에 20배 폭증 → 5분 유지 → 감소 → 두 번째 폭증
// 요청 비율: 읽기 80%, 쓰기 15%, 로그인 5%
import exec from 'k6/execution';

import { createUsers, login, read, write } from './lib.js';

const QUICK = __ENV.QUICK === '1';
// PROFILE=local: 노트북(Docker ~3.8GiB/6CPU)용 축소판. 모양·단계·임계치는 그대로다.
//   USERS 500→60 (약 1/8), BASELINE 20→4 (1/5), PEAK 400→80 (1/5, 기본값 BASELINE×20)
// 개별 값은 언제든 -e USERS=.. -e BASELINE=.. -e PEAK=.. 로 덮어쓴다. QUICK=1 은 시간만 줄이는 별개 옵션이다.
const LOCAL = __ENV.PROFILE === 'local';
const USERS = parseInt(__ENV.USERS || (QUICK ? '20' : LOCAL ? '60' : '500'), 10);
const BASELINE = parseInt(__ENV.BASELINE || (QUICK ? '5' : LOCAL ? '4' : '20'), 10); // 초당 요청 수
const PEAK = parseInt(__ENV.PEAK || String(BASELINE * 20), 10);

function stages() {
  const d = (full) => (QUICK ? '10s' : full);
  return [
    { target: BASELINE, duration: d('1m') }, // 평상시
    { target: PEAK, duration: d('30s') }, // 1차 폭증
    { target: PEAK, duration: d('5m') },
    { target: BASELINE, duration: d('1m') }, // 감소
    { target: BASELINE, duration: d('2m') },
    { target: PEAK, duration: d('30s') }, // 2차 폭증
    { target: PEAK, duration: d('3m') },
    { target: 0, duration: d('1m') },
  ];
}

export const options = {
  setupTimeout: '10m',
  scenarios: {
    disaster: {
      executor: 'ramping-arrival-rate',
      startRate: BASELINE,
      timeUnit: '1s',
      preAllocatedVUs: Math.max(50, PEAK),
      maxVUs: PEAK * 5,
      stages: stages(),
    },
  },
  thresholds: {
    read_duration: ['p(95)<300'],
    write_accepted_duration: ['p(95)<100'],
    server_errors: ['rate<0.01'],
  },
};

export function setup() {
  return { users: createUsers(USERS) };
}

export default function (data) {
  // 반복 번호로 사용자를 고르게 돌려 써서, 세션 쿠키(sid)별 글쓰기 리밋(2r/s)에 걸리지 않게 한다.
  // 사용자마다 sid가 하나이므로 사용자별 한도와 같다.
  // 예: PEAK 400r/s × 쓰기 15% ÷ 500명 = 사용자당 0.12r/s
  const user = data.users[exec.scenario.iterationInTest % data.users.length];
  const r = Math.random();
  if (r < 0.8) read();
  else if (r < 0.95) write(user);
  else login(user);
}
