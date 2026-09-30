// 재난 상황 트래픽 시뮬레이션 (spec §11)
// 평상시 → 30초 만에 20배 폭증 → 5분 유지 → 감소 → 두 번째 폭증
// 요청 비율: 읽기 80%, 쓰기 15%, 로그인 5%
import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const QUICK = __ENV.QUICK === '1';
// PROFILE=local: 노트북(Docker ~3.8GiB/6CPU)용 축소판. 클라우드 기본값의 약 1/5 규모다.
//   USERS 500→60, BASELINE 20→4, PEAK 400→80 (모양·단계·임계치는 그대로)
// 개별 값은 언제든 -e USERS=.. -e BASELINE=.. -e PEAK=.. 로 덮어쓴다. QUICK=1 은 시간만 줄이는 별개 옵션이다.
const LOCAL = __ENV.PROFILE === 'local';
const USERS = parseInt(__ENV.USERS || (QUICK ? '20' : LOCAL ? '60' : '500'), 10);
const BASELINE = parseInt(__ENV.BASELINE || (QUICK ? '5' : LOCAL ? '4' : '20'), 10); // 초당 요청 수
const PEAK = parseInt(__ENV.PEAK || String(BASELINE * 20), 10);
const PASSWORD = 'loadtest-Passw0rd!';
const JSON_HEADERS = { 'Content-Type': 'application/json' };
// auth는 argon2 해시를 Pod당 4개까지만 동시에 처리하므로 가입·로그인 배치도 4개씩 보낸다.
const SIGNUP_BATCH = 4;

const readDuration = new Trend('read_duration', true);
const writeAcceptedDuration = new Trend('write_accepted_duration', true);
const serverErrors = new Rate('server_errors');
const backpressure = new Counter('backpressure_503');
const rateLimited = new Counter('rate_limited_429');

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

// 요청마다 새 쿠키 저장소를 써서 VU 사이에 세션이 섞이지 않게 한다.
function params(extra = {}) {
  return { headers: JSON_HEADERS, jar: new http.CookieJar(), ...extra };
}

function errorCode(res) {
  try {
    return res.json('code');
  } catch (e) {
    return null;
  }
}

// 백프레셔 503(QUEUE_FULL)은 설계된 동작이므로 서버 에러와 따로 집계한다.
// nginx 리밋 초과는 429 {code:"RATE_LIMITED"} (Retry-After: 2)로 온다.
function record(res) {
  if (res.status === 429 && errorCode(res) === 'RATE_LIMITED') rateLimited.add(1);
  const isBackpressure = res.status === 503 && errorCode(res) === 'QUEUE_FULL';
  if (isBackpressure) backpressure.add(1);
  serverErrors.add(res.status >= 500 && !isBackpressure);
}

export function setup() {
  const runId = Date.now().toString(36);
  const users = [];
  for (let i = 0; i < USERS; i++) {
    users.push({ email: `lt-${runId}-${i}@example.com`, nickname: `lt${runId}${i}` });
  }

  for (let i = 0; i < users.length; i += SIGNUP_BATCH) {
    const chunk = users.slice(i, i + SIGNUP_BATCH);

    const signups = http.batch(
      chunk.map((u) => [
        'POST',
        `${BASE_URL}/api/auth/signup`,
        JSON.stringify({ email: u.email, password: PASSWORD, nickname: u.nickname }),
        params(),
      ]),
    );
    signups.forEach((res, j) => {
      if (res.status !== 201) {
        throw new Error(`signup failed for ${chunk[j].email}: ${res.status} ${res.body}`);
      }
    });

    const logins = http.batch(
      chunk.map((u) => [
        'POST',
        `${BASE_URL}/api/auth/login`,
        JSON.stringify({ email: u.email, password: PASSWORD }),
        params(),
      ]),
    );
    logins.forEach((res, j) => {
      const cookie = res.cookies.sid;
      if (res.status !== 200 || !cookie || cookie.length === 0) {
        throw new Error(`login failed for ${chunk[j].email}: ${res.status} ${res.body}`);
      }
      chunk[j].sid = cookie[0].value;
    });
  }

  return { users: users.map((u) => ({ email: u.email, sid: u.sid })) };
}

function read() {
  const res = http.get(`${BASE_URL}/api/board/posts`, params({ tags: { op: 'read' } }));
  record(res);
  check(res, { 'read 200': (r) => r.status === 200 });
  if (res.status !== 200) return;
  readDuration.add(res.timings.duration);

  // 4번 중 1번은 다음 페이지까지 본다(캐시되지 않는 DB 조회 경로).
  const cursor = res.json('next_cursor');
  if (cursor && Math.random() < 0.25) {
    const next = http.get(
      `${BASE_URL}/api/board/posts?cursor=${encodeURIComponent(cursor)}`,
      params({ tags: { op: 'read_next' } }),
    );
    record(next);
    if (next.status === 200) readDuration.add(next.timings.duration);
  }
}

function write(user) {
  const res = http.post(
    `${BASE_URL}/api/board/posts`,
    JSON.stringify({
      title: `재난 상황 공유 ${uuidv4().slice(0, 8)}`,
      body: '현재 위치 상황을 공유합니다. (loadtest)',
    }),
    params({
      headers: { ...JSON_HEADERS, 'Idempotency-Key': uuidv4(), Cookie: `sid=${user.sid}` },
      tags: { op: 'write' },
    }),
  );
  record(res);
  check(res, { 'write 202': (r) => r.status === 202 });
  if (res.status === 202) writeAcceptedDuration.add(res.timings.duration);
}

function login(user) {
  const res = http.post(
    `${BASE_URL}/api/auth/login`,
    JSON.stringify({ email: user.email, password: PASSWORD }),
    params({ tags: { op: 'login' } }),
  );
  record(res);
  check(res, { 'login 200': (r) => r.status === 200 });
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
