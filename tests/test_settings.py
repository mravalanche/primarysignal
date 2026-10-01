import pytest
from pydantic import ValidationError

from primary_signal.config import LogLevel, RuntimeEnvironment, Settings


def test_settings_have_safe_defaults() -> None:
    settings = Settings()

    assert settings.environment is RuntimeEnvironment.DEVELOPMENT
    assert settings.debug is False
    assert settings.log_level is LogLevel.INFO
    assert settings.api_docs_enabled is False


def test_settings_load_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setenv("PRIMARY_SIGNAL_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("PRIMARY_SIGNAL_API_DOCS_ENABLED", "true")

    settings = Settings()

    assert settings.environment is RuntimeEnvironment.TEST
    assert settings.log_level is LogLevel.WARNING
    assert settings.api_docs_enabled is True


def test_settings_reject_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "staging")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("unsafe_option", ["DEBUG", "API_DOCS_ENABLED"])
def test_settings_reject_developer_diagnostics_in_production(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_option: str,
) -> None:
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "production")
    monkeypatch.setenv(f"PRIMARY_SIGNAL_{unsafe_option}", "true")

    with pytest.raises(ValidationError, match="must be disabled in production"):
        Settings()
