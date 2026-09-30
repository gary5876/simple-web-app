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
