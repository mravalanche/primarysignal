"""Typed application settings with safe local defaults."""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeEnvironment(StrEnum):
    """Supported runtime modes."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    """Supported application log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Non-secret process settings loaded from ``PRIMARY_SIGNAL_*`` variables.

    Credential and deployment-secret fields are deliberately absent until the
    components that consume them are implemented.
    """

    model_config = SettingsConfigDict(
        env_prefix="PRIMARY_SIGNAL_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    environment: RuntimeEnvironment = RuntimeEnvironment.DEVELOPMENT
    debug: bool = False
    log_level: LogLevel = LogLevel.INFO
    api_docs_enabled: bool = False
    service_name: str = Field(default="Primary Signal", min_length=1, max_length=80)

    @model_validator(mode="after")
    def reject_unsafe_production_options(self) -> Self:
        """Keep developer diagnostics out of production by construction."""

        if self.environment is RuntimeEnvironment.PRODUCTION and (
            self.debug or self.api_docs_enabled
        ):
            raise ValueError("debug and API documentation must be disabled in production")
        return self


class SchedulerSettings(BaseSettings):
    """Tuning for the feed scheduler process."""

    model_config = SettingsConfigDict(
        env_prefix="PRIMARY_SIGNAL_SCHEDULER_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    batch_size: int = Field(default=100, ge=1, le=500)
    poll_interval_seconds: float = Field(default=30.0, ge=0.1, le=3_600.0)
    retry_initial_seconds: float = Field(default=1.0, ge=0.1, le=3_600.0)
    retry_max_seconds: float = Field(default=60.0, ge=0.1, le=3_600.0)

    @model_validator(mode="after")
    def require_ordered_retry_bounds(self) -> Self:
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ValueError("scheduler retry initial delay must not exceed its maximum")
        return self


class ProcessorSettings(BaseSettings):
    """Bounded, non-secret controls for the ingestion processor."""

    model_config = SettingsConfigDict(
        env_prefix="PRIMARY_SIGNAL_PROCESSOR_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    recovery_batch_size: int = Field(default=100, ge=1, le=1000)
    poll_interval_seconds: float = Field(default=5.0, ge=0.1, le=3600.0)
    lease_seconds: int = Field(default=120, ge=5, le=3600)
    heartbeat_interval_seconds: float = Field(default=30.0, ge=0.1, le=600.0)
    retry_initial_seconds: float = Field(default=1.0, ge=0.1, le=3600.0)
    retry_max_seconds: float = Field(default=60.0, ge=0.1, le=3600.0)

    @model_validator(mode="after")
    def require_ordered_intervals(self) -> Self:
        if self.heartbeat_interval_seconds >= self.lease_seconds / 2:
            raise ValueError("processor heartbeat interval must be less than half the lease")
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ValueError("processor retry initial delay must not exceed its maximum")
        return self
