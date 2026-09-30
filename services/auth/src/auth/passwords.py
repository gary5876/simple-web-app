import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool

_hasher = PasswordHasher()

# argon2 는 해시 하나당 메모리를 많이(기본 약 64MiB) 쓴다. 스레드풀이 허용하는 만큼
# 동시에 돌리면 파드가 OOMKill 될 수 있어서, 프로세스 전체에서 동시 실행 수를 제한한다.
_DEFAULT_HASH_CONCURRENCY = 4
_semaphore = asyncio.Semaphore(_DEFAULT_HASH_CONCURRENCY)


def configure(concurrency: int) -> None:
    """앱 시작 시 호출해서 동시 argon2 실행 수 제한을 (다시) 설정한다."""
    global _semaphore
    _semaphore = asyncio.Semaphore(concurrency)

# 존재하지 않는 이메일로 로그인할 때도 같은 시간만큼 해시 검증을 해서,
# 응답 시간으로 가입 여부를 추측하지 못하게 한다.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


async def hash_password(password: str) -> str:
    # argon2 는 CPU 를 많이 쓰므로 이벤트 루프를 막지 않게 스레드풀에서 실행한다.
    async with _semaphore:
        return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    async with _semaphore:
        return await run_in_threadpool(_verify)
