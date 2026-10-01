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
