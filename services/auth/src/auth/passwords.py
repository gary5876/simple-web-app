from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool

_hasher = PasswordHasher()

# 존재하지 않는 이메일로 로그인할 때도 같은 시간만큼 해시 검증을 해서,
# 응답 시간으로 가입 여부를 추측하지 못하게 한다.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


async def hash_password(password: str) -> str:
    # argon2 는 CPU 를 많이 쓰므로 이벤트 루프를 막지 않게 스레드풀에서 실행한다.
    return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    return await run_in_threadpool(_verify)
