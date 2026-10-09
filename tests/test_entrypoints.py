from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

import pytest
from sqlalchemy import Engine

from primary_signal.db import DatabaseSettings
from primary_signal.entrypoints import migrate, processor, retriever, scheduler, web


def test_retriever_entrypoint_is_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        app: object,
        *,
        host: str,
        port: int,
        proxy_headers: bool,
        access_log: bool,
        log_config: object,
    ) -> None:
        captured.update(
            app=app,
            host=host,
            port=port,
            proxy_headers=proxy_headers,
            access_log=access_log,
            log_config=log_config,
        )

    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "development")
    monkeypatch.setattr(retriever.uvicorn, "run", fake_run)
    with pytest.raises(SystemExit):
        retriever.main([])
    retriever.main(["--allow-local-fetch"])
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["proxy_headers"] is False
    assert captured["access_log"] is False
    assert captured["log_config"] is None

    with pytest.raises(SystemExit):
        retriever.main(["--allow-local-fetch", "--host", "0.0.0.0"])  # noqa: S104
    with pytest.raises(SystemExit):
        retriever.main(["--allow-local-fetch", "--port", "0"])
    monkeypatch.delenv("PRIMARY_SIGNAL_ENVIRONMENT")
    with pytest.raises(SystemExit):
        retriever.main(["--allow-local-fetch"])
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "production")
    with pytest.raises(SystemExit):
        retriever.main(["--allow-local-fetch"])


def test_processor_entrypoint_rejects_unbound_ingestion_queue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        processor.main(["--once"])

    assert raised.value.code == 1
    error_output = capsys.readouterr().err
    assert '"event":"processor.failed"' in error_output
    assert '"error_type":"UnknownJobHandler"' in error_output
    assert "feeds.poll" not in error_output


def test_web_entrypoint_runs_selected_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        app: object,
        *,
        host: str,
        port: int,
        proxy_headers: bool,
        log_config: object,
    ) -> None:
        captured.update(
            app=app,
            host=host,
            port=port,
            proxy_headers=proxy_headers,
            log_config=log_config,
        )

    monkeypatch.setattr(web.uvicorn, "run", fake_run)

    web.main(["--surface", "public", "--host", "127.0.0.2", "--port", "8080"])

    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8080
    assert captured["proxy_headers"] is False
    assert captured["log_config"] is None
    assert captured["app"].state.surface == "public"  # type: ignore[union-attr]

    web.main(["--surface", "admin"])
    assert captured["proxy_headers"] is False
    assert captured["app"].state.surface == "admin"  # type: ignore[union-attr]


def test_public_production_entrypoint_uses_restricted_database_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            captured["connected"] = True
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: object) -> object:
            captured["capability_checked"] = True

            class RestrictedResult:
                def scalar_one(self) -> bool:
                    return True

            return RestrictedResult()

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

        def dispose(self) -> None:
            captured["disposed"] = True

    engine = cast(Engine, FakeEngine())
    reader = object()

    def fake_create_engine(settings: DatabaseSettings, *, search_path: str) -> Engine:
        captured["database_settings"] = settings
        captured["search_path"] = search_path
        return engine

    def fake_run(app: object, **_kwargs: object) -> None:
        captured["app"] = app

    def fake_reader(actual_engine: Engine) -> object:
        assert actual_engine is engine
        return reader

    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_DATABASE_URL",
        "postgresql+psycopg://public_reader@db.public.example/app",
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "public_reader")
    monkeypatch.setattr(web, "create_database_engine", fake_create_engine)
    monkeypatch.setattr(web, "PostgresStoryReader", fake_reader)
    monkeypatch.setattr(web.uvicorn, "run", fake_run)

    web.main(["--surface", "public"])

    assert captured["connected"] is True
    assert captured["capability_checked"] is True
    assert captured["disposed"] is True
    assert captured["search_path"] == "pg_catalog"
    assert captured["app"].state.story_reader is reader  # type: ignore[union-attr]
    database_settings = cast(DatabaseSettings, captured["database_settings"])
    assert database_settings.expected_role == "public_reader"
    assert database_settings.application_name == "primary_signal_public_web"
    assert database_settings.max_overflow == 0


def test_public_production_entrypoint_disposes_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeEngine:
        def connect(self) -> None:
            raise RuntimeError("private connection detail")

        def dispose(self) -> None:
            captured["disposed"] = True

    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_DATABASE_URL",
        "postgresql+psycopg://public_reader@db.public.example/app",
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "public_reader")

    def fake_create_engine(_settings: DatabaseSettings, *, search_path: str) -> Engine:
        assert search_path == "pg_catalog"
        return cast(Engine, FakeEngine())

    monkeypatch.setattr(web, "create_database_engine", fake_create_engine)

    with pytest.raises(SystemExit) as raised:
        web.main(["--surface", "public"])

    assert raised.value.code == 1
    assert captured["disposed"] is True
    error_output = capsys.readouterr().err
    assert "web.failed" in error_output
    assert "private connection detail" not in error_output


def test_migrate_entrypoint_upgrades_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_upgrade(config: object, revision: str) -> None:
        captured.update(config=config, revision=revision)

    monkeypatch.setattr(migrate.command, "upgrade", fake_upgrade)

    migrate.main([])

    assert captured["revision"] == "head"
    assert migrate.migration_config_path().is_file()


def test_scheduler_entrypoint_composes_once_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeEngine:
        def dispose(self) -> None:
            captured["disposed"] = True

    engine = cast(Engine, FakeEngine())

    @contextmanager
    def fake_signals(_stop_event: object) -> Generator[None]:
        captured["signals"] = True
        yield

    def fake_run(
        actual_engine: Engine,
        settings: object,
        stop_event: object,
        *,
        once: bool,
    ) -> None:
        captured.update(
            engine=actual_engine,
            settings=settings,
            stop_event=stop_event,
            once=once,
        )

    monkeypatch.setenv(
        "PRIMARY_SIGNAL_DATABASE_URL",
        "postgresql+psycopg://scheduler@db.public.example/app",
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "scheduler")

    def fake_create_engine(_settings: DatabaseSettings) -> Engine:
        return engine

    monkeypatch.setattr(scheduler, "create_database_engine", fake_create_engine)
    monkeypatch.setattr(scheduler, "stopping_on_signals", fake_signals)
    monkeypatch.setattr(scheduler, "run_with_engine", fake_run)

    scheduler.main(["--once"])

    assert captured["engine"] is engine
    assert captured["once"] is True
    assert captured["signals"] is True
    assert captured["disposed"] is True
    database_settings = scheduler.scheduler_database_settings()
    assert database_settings.application_name == "primary_signal_scheduler"
    assert database_settings.pool_size == 1
    assert database_settings.max_overflow == 0


def test_scheduler_entrypoint_disposes_engine_after_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disposed = False

    class FakeEngine:
        def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    @contextmanager
    def fake_signals(_stop_event: object) -> Generator[None]:
        yield

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic invariant")

    monkeypatch.setenv(
        "PRIMARY_SIGNAL_DATABASE_URL",
        "postgresql+psycopg://scheduler@db.public.example/app",
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "scheduler")

    def fake_create_engine(_settings: DatabaseSettings) -> Engine:
        return cast(Engine, FakeEngine())

    monkeypatch.setattr(scheduler, "create_database_engine", fake_create_engine)
    monkeypatch.setattr(scheduler, "stopping_on_signals", fake_signals)
    monkeypatch.setattr(scheduler, "run_with_engine", fail_run)

    with pytest.raises(SystemExit) as raised:
        scheduler.main([])

    assert raised.value.code == 1
    assert disposed is True
    error_output = capsys.readouterr().err
    assert "scheduler.failed" in error_output
    assert '"error_type":"RuntimeError"' in error_output
    assert "synthetic invariant" not in error_output


def test_scheduler_entrypoint_sanitizes_disposal_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeEngine:
        def dispose(self) -> None:
            raise RuntimeError("private disposal detail")

    @contextmanager
    def fake_signals(_stop_event: object) -> Generator[None]:
        yield

    def fake_create_engine(_settings: DatabaseSettings) -> Engine:
        return cast(Engine, FakeEngine())

    def fake_run(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setenv(
        "PRIMARY_SIGNAL_DATABASE_URL",
        "postgresql+psycopg://scheduler@db.public.example/app",
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "scheduler")
    monkeypatch.setattr(scheduler, "create_database_engine", fake_create_engine)
    monkeypatch.setattr(scheduler, "stopping_on_signals", fake_signals)
    monkeypatch.setattr(scheduler, "run_with_engine", fake_run)

    with pytest.raises(SystemExit) as raised:
        scheduler.main(["--once"])

    assert raised.value.code == 1
    error_output = capsys.readouterr().err
    assert "scheduler.dispose.failed" in error_output
    assert "private disposal detail" not in error_output
