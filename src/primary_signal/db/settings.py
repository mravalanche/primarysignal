"""Typed, PostgreSQL-only database settings."""

import os
import stat
from typing import Any, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MAX_DATABASE_URL_BYTES = 4096


def _read_database_url_file(path: str) -> SecretStr:
    """Read one bounded UTF-8 secret without exposing its path or contents."""

    try:
        with open(path, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("database URL file is invalid")
            raw = stream.read(MAX_DATABASE_URL_BYTES + 3)
    except OSError:
        raise ValueError("database URL file could not be read") from None
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw or len(raw) > MAX_DATABASE_URL_BYTES or b"\n" in raw or b"\r" in raw:
        raise ValueError("database URL file must contain one bounded line")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("database URL file must be UTF-8") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise ValueError("database URL file contains control characters")
    return SecretStr(decoded)


class DatabaseSettings(BaseSettings):
    """Database connection settings loaded only by database-using processes."""

    model_config = SettingsConfigDict(
        env_prefix="PRIMARY_SIGNAL_DATABASE_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    def __init__(self, **data: Any) -> None:
        prefix = str(self.model_config.get("env_prefix", "PRIMARY_SIGNAL_DATABASE_"))
        file_path = os.environ.get(f"{prefix}URL_FILE")
        if file_path is not None:
            if "url" in data or os.environ.get(f"{prefix}URL") is not None:
                raise ValueError("database URL and URL_FILE cannot both be set")
            if not file_path:
                raise ValueError("database URL file is invalid")
            data["url"] = _read_database_url_file(file_path)
        super().__init__(**data)

    url: SecretStr
    expected_role: str = Field(min_length=1, max_length=63, pattern=r"^[a-z_][a-z0-9_]*$")
    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=5, ge=0, le=20)
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    lock_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    idle_transaction_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    application_name: str = Field(
        default="primary_signal",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z_][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def require_postgresql_psycopg(self) -> Self:
        """Reject accidental SQLite use and ambiguous PostgreSQL drivers."""

        try:
            driver_name = make_url(self.url.get_secret_value()).drivername
        except ArgumentError:
            raise ValueError("database URL is invalid") from None
        if driver_name != "postgresql+psycopg":
            raise ValueError("database URL must use postgresql+psycopg")
        return self
