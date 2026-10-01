"""Typed, PostgreSQL-only database settings."""

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class DatabaseSettings(BaseSettings):
    """Database connection settings loaded only by database-using processes."""

    model_config = SettingsConfigDict(
        env_prefix="PRIMARY_SIGNAL_DATABASE_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

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
        except ArgumentError as error:
            raise ValueError("database URL is invalid") from error
        if driver_name != "postgresql+psycopg":
            raise ValueError("database URL must use postgresql+psycopg")
        return self
