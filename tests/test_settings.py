import pytest
from pydantic import ValidationError

from primary_signal.config import LogLevel, RuntimeEnvironment, SchedulerSettings, Settings


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


def test_scheduler_settings_have_bounded_defaults() -> None:
    settings = SchedulerSettings()

    assert settings.batch_size == 100
    assert settings.poll_interval_seconds == 30
    assert settings.retry_initial_seconds == 1
    assert settings.retry_max_seconds == 60


def test_scheduler_settings_load_their_own_environment_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIMARY_SIGNAL_SCHEDULER_BATCH_SIZE", "250")
    monkeypatch.setenv("PRIMARY_SIGNAL_SCHEDULER_POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("PRIMARY_SIGNAL_BATCH_SIZE", "12")

    settings = SchedulerSettings()

    assert settings.batch_size == 250
    assert settings.poll_interval_seconds == 0.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BATCH_SIZE", "0"),
        ("BATCH_SIZE", "501"),
        ("POLL_INTERVAL_SECONDS", "0"),
        ("POLL_INTERVAL_SECONDS", "3601"),
        ("RETRY_INITIAL_SECONDS", "0"),
        ("RETRY_MAX_SECONDS", "3601"),
    ],
)
def test_scheduler_settings_reject_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(f"PRIMARY_SIGNAL_SCHEDULER_{name}", value)

    with pytest.raises(ValidationError):
        SchedulerSettings()


def test_scheduler_settings_require_retry_initial_not_above_maximum() -> None:
    with pytest.raises(ValidationError, match="initial delay must not exceed"):
        SchedulerSettings(retry_initial_seconds=2, retry_max_seconds=1)
