// disaster.js와 steps.js가 함께 쓰는 요청 함수와 메트릭.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

export const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const PASSWORD = 'loadtest-Passw0rd!';
const JSON_HEADERS = { 'Content-Type': 'application/json' };
// auth는 argon2 해시를 Pod당 4개까지만 동시에 처리하므로 가입·로그인 배치도 4개씩 보낸다.
const SIGNUP_BATCH = 4;

export const readDuration = new Trend('read_duration', true);
export const writeAcceptedDuration = new Trend('write_accepted_duration', true);
export const serverErrors = new Rate('server_errors');
const backpressure = new Counter('backpressure_503');
const rateLimited = new Counter('rate_limited_429');

// 요청마다 새 쿠키 저장소를 써서 VU 사이에 세션이 섞이지 않게 한다.
// X-Load-Op/X-Load-Step 헤더는 nginx perf 로그에 남아 analyze.py가 요청을 op·단계별로 묶는 데 쓴다.
function params(op, step, extra = {}) {
  const tags = { op, ...(step ? { step: String(step) } : {}) };
  const headers = { ...JSON_HEADERS, 'X-Load-Op': op, ...(step ? { 'X-Load-Step': String(step) } : {}) };
  return { jar: new http.CookieJar(), ...extra, tags, headers: { ...headers, ...(extra.headers || {}) } };
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

// 사용자 count명을 가입·로그인시켜 세션 쿠키(sid)를 받아 둔다.
export function createUsers(count) {
  const runId = Date.now().toString(36);
  const users = [];
  for (let i = 0; i < count; i++) {
    users.push({ email: `lt-${runId}-${i}@example.com`, nickname: `lt${runId}${i}` });
  }

  for (let i = 0; i < users.length; i += SIGNUP_BATCH) {
    const chunk = users.slice(i, i + SIGNUP_BATCH);

    const signups = http.batch(
      chunk.map((u) => [
        'POST',
        `${BASE_URL}/api/auth/signup`,
        JSON.stringify({ email: u.email, password: PASSWORD, nickname: u.nickname }),
        params('setup'),
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
        params('setup'),
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

  return users.map((u) => ({ email: u.email, sid: u.sid }));
}

export function read(step) {
  const res = http.get(`${BASE_URL}/api/board/posts`, params('read', step));
  record(res);
  check(res, { 'read 200': (r) => r.status === 200 });
  if (res.status !== 200) return;
  readDuration.add(res.timings.duration);

  // 4번 중 1번은 다음 페이지까지 본다(캐시되지 않는 DB 조회 경로).
  const cursor = res.json('next_cursor');
  if (cursor && Math.random() < 0.25) {
    const next = http.get(`${BASE_URL}/api/board/posts?cursor=${encodeURIComponent(cursor)}`, params('read_next', step));
    record(next);
    if (next.status === 200) readDuration.add(next.timings.duration);
  }
}

export function write(user, step) {
  const res = http.post(
    `${BASE_URL}/api/board/posts`,
    JSON.stringify({
      title: `재난 상황 공유 ${uuidv4().slice(0, 8)}`,
      body: '현재 위치 상황을 공유합니다. (loadtest)',
    }),
    params('write', step, { headers: { 'Idempotency-Key': uuidv4(), Cookie: `sid=${user.sid}` } }),
  );
  record(res);
  check(res, { 'write 202': (r) => r.status === 202 });
  if (res.status === 202) writeAcceptedDuration.add(res.timings.duration);
}

export function login(user, step) {
  const res = http.post(
    `${BASE_URL}/api/auth/login`,
    JSON.stringify({ email: user.email, password: PASSWORD }),
    params('login', step),
  );
  record(res);
  check(res, { 'login 200': (r) => r.status === 200 });
}
