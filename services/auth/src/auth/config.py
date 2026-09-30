from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    session_ttl_seconds: int = 86400
    cookie_secure: bool = True
    log_level: str = "INFO"
    login_fail_limit: int = 10
    login_fail_window_seconds: int = 900
    hash_concurrency: int = 4
    # false면 DB 장애가 readiness를 깨지 않는다(세션 검증 전용 auth-verify 배포용).
    ready_requires_db: bool = True
