// 계단식 부하: 초당 요청 수(rps)를 단계별로 고정해 두고, 단계마다 응답시간을 층별로 나눠 보기 위한 시나리오.
// 각 단계 사이에 GAP만큼 쉬어서 앞 단계의 대기열이 다음 단계 측정에 섞이지 않게 한다.
// 결과 분석: loadtest/analyze.py (nginx perf 로그 + 이 스크립트의 요약 JSON)
//
// 환경 변수:
//   RATES    단계별 rps, 쉼표 구분 (기본 5,20,50,100)
//   STEP     단계 길이 (기본 60s)
//   GAP      단계 사이 쉬는 시간 (기본 10s)
//   MIX      읽기,쓰기,로그인 비율(%) (기본 80,15,5). 예: MIX=100,0,0 이면 읽기만
//   USERS    setup에서 가입·로그인할 사용자 수 (기본 60)
//   SUMMARY  요약 JSON 경로 (기본 loadtest/results/steps-summary.json)
import exec from 'k6/execution';

import { createUsers, login, read, write } from './lib.js';

const RATES = (__ENV.RATES || '5,20,50,100').split(',').map((v) => parseInt(v, 10));
const STEP_SECONDS = parseInt(__ENV.STEP || '60', 10);
const GAP_SECONDS = parseInt(__ENV.GAP || '10', 10);
const [READ_PCT, WRITE_PCT] = (__ENV.MIX || '80,15,5').split(',').map((v) => parseInt(v, 10));
const USERS = parseInt(__ENV.USERS || '60', 10);
const SUMMARY = __ENV.SUMMARY || 'loadtest/results/steps-summary.json';
const OPS = ['read', 'read_next', 'write', 'login'];

function scenarios() {
  const result = {};
  RATES.forEach((rate, i) => {
    result[`step_${rate}`] = {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration: `${STEP_SECONDS}s`,
      startTime: `${i * (STEP_SECONDS + GAP_SECONDS)}s`,
      preAllocatedVUs: Math.max(10, rate),
      // 응답이 느려지면 동시에 떠 있는 요청이 늘어난다(리틀의 법칙). 모자라면 k6가 dropped_iterations로 센다.
      maxVUs: rate * 20 + 50,
      env: { LOAD_STEP: String(rate) },
    };
  });
  return result;
}

// 단계 x op 조합마다 항상 통과하는 threshold를 걸어 둔다. 이렇게 해야 요약 JSON에 조합별 수치가 나온다.
function thresholds() {
  const result = {};
  for (const rate of RATES) {
    for (const op of OPS) {
      for (const metric of ['http_req_duration', 'http_req_waiting', 'http_req_connecting']) {
        result[`${metric}{step:${rate},op:${op}}`] = ['max>=0'];
      }
    }
  }
  return result;
}

export const options = {
  setupTimeout: '10m',
  scenarios: scenarios(),
  thresholds: thresholds(),
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max', 'count'],
};

export function setup() {
  return { users: createUsers(USERS) };
}

export default function (data) {
  const step = __ENV.LOAD_STEP;
  const user = data.users[exec.scenario.iterationInTest % data.users.length];
  const r = Math.random() * 100;
  if (r < READ_PCT) read(step);
  else if (r < READ_PCT + WRITE_PCT) write(user, step);
  else login(user, step);
}

export function handleSummary(data) {
  return {
    [SUMMARY]: JSON.stringify(data, null, 2),
    stdout: `\n요약 저장: ${SUMMARY}\n`,
  };
}
