"""Private administration authentication settings."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from primary_signal.web.admin.auth import AdminAuthConfig


class AdminAuthSettings(BaseSettings):
    """Loaded only by the admin process from deployment-owned environment."""

    model_config = SettingsConfigDict(
        env_prefix="PRIMARY_SIGNAL_ADMIN_", case_sensitive=False, extra="ignore", frozen=True
    )

    origin: str
    password_phc: SecretStr
    session_key_hex: SecretStr
    idle_seconds: int = Field(default=1800, ge=60, le=86400)
    absolute_seconds: int = Field(default=43200, ge=60, le=86400)
    source_attempts: int = Field(default=5, ge=1, le=100)
    global_attempts: int = Field(default=20, ge=1, le=1000)
    attempt_window_seconds: int = Field(default=900, ge=60, le=86400)

    def auth_config(self) -> AdminAuthConfig:
        try:
            key = bytes.fromhex(self.session_key_hex.get_secret_value())
        except ValueError as error:
            raise ValueError("admin session key must be hexadecimal") from error
        return AdminAuthConfig(
            origin=self.origin,
            password_phc=self.password_phc.get_secret_value(),
            session_key=key,
            idle_seconds=self.idle_seconds,
            absolute_seconds=self.absolute_seconds,
            source_attempts=self.source_attempts,
            global_attempts=self.global_attempts,
            attempt_window_seconds=self.attempt_window_seconds,
        )
