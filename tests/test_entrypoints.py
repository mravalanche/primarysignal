from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Engine

from primary_signal.db import DatabaseSettings
from primary_signal.entrypoints import migrate, processor, retriever, scheduler, web
from primary_signal.ingestion.feed_handler import FeedFetchError
from primary_signal.retrieval.protocol import ARTICLE_FETCH_CAPABILITY, FEED_FETCH_CAPABILITY


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


def test_processor_entrypoint_requires_local_opt_in_and_rejects_production(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["--once", "--queue", "ingestion", "--retriever-origin", "http://127.0.0.1:8765"]
    with pytest.raises(SystemExit) as raised:
        processor.main(arguments)
    assert raised.value.code == 2
    monkeypatch.delenv("PRIMARY_SIGNAL_ENVIRONMENT", raising=False)
    with pytest.raises(SystemExit) as raised:
        processor.main([*arguments, "--allow-local-fetch"])
    assert raised.value.code == 1
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "production")
    with pytest.raises(SystemExit) as raised:
        processor.main([*arguments, "--allow-local-fetch"])
    assert raised.value.code == 1
    assert '"event":"processor.failed"' in capsys.readouterr().err


@pytest.mark.parametrize("queue_name", ["ingestion", "retrieval"])
def test_processor_entrypoint_binds_selected_queue(
    monkeypatch: pytest.MonkeyPatch, queue_name: str
) -> None:
    from unittest.mock import MagicMock

    engine = MagicMock(spec=Engine)
    retriever_client = MagicMock()
    retriever_client.__enter__.return_value = retriever_client
    handlers = object()
    captured: dict[str, object] = {}

    def fake_client(origin: str) -> MagicMock:
        captured["origin"] = origin
        return retriever_client

    def fake_handlers(actual_engine: Engine, client: object) -> object:
        assert actual_engine is engine
        assert client is retriever_client
        retriever_client.require_ready.assert_called_once_with(
            FEED_FETCH_CAPABILITY if queue_name == "ingestion" else ARTICLE_FETCH_CAPABILITY
        )
        captured["binding"] = queue_name
        return handlers

    def fake_run(
        actual_engine: Engine,
        _settings: object,
        _stop_event: object,
        actual_handlers: object,
        *,
        once: bool,
        queue_name: str,
    ) -> None:
        assert actual_engine is engine
        assert actual_handlers is handlers
        captured["run"] = (once, queue_name)

    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setattr(processor, "processor_database_settings", lambda: object())

    def fake_create_engine(_settings: object) -> Engine:
        return engine

    monkeypatch.setattr(processor, "create_database_engine", fake_create_engine)
    monkeypatch.setattr(processor, "RetrieverClient", fake_client)
    monkeypatch.setattr(processor, "run_with_engine", fake_run)
    monkeypatch.setattr(
        processor,
        "build_feed_poll_handlers"
        if queue_name == "ingestion"
        else "build_article_retrieval_handlers",
        fake_handlers,
    )

    processor.main(
        [
            "--once",
            "--queue",
            queue_name,
            "--retriever-origin",
            "http://127.0.0.1:8765",
            "--allow-local-fetch",
        ]
    )

    assert captured == {
        "origin": "http://127.0.0.1:8765",
        "binding": queue_name,
        "run": (True, queue_name),
    }
    engine.dispose.assert_called_once()


@pytest.mark.parametrize("queue_name", ["ingestion", "retrieval"])
def test_processor_readiness_failure_prevents_handler_binding_and_queue_runtime(
    monkeypatch: pytest.MonkeyPatch, queue_name: str
) -> None:
    from unittest.mock import MagicMock

    engine = MagicMock(spec=Engine)
    client = MagicMock()
    client.__enter__.return_value = client
    client.require_ready.side_effect = FeedFetchError("dependency_unavailable")
    runtime = MagicMock()
    feed_handlers, article_handlers = MagicMock(), MagicMock()
    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "test")
    monkeypatch.setattr(processor, "processor_database_settings", lambda: object())

    def create_engine(_settings: object) -> Engine:
        return engine

    def create_client(_origin: str) -> MagicMock:
        return client

    monkeypatch.setattr(processor, "create_database_engine", create_engine)
    monkeypatch.setattr(processor, "RetrieverClient", create_client)
    monkeypatch.setattr(processor, "run_with_engine", runtime)
    monkeypatch.setattr(processor, "build_feed_poll_handlers", feed_handlers)
    monkeypatch.setattr(processor, "build_article_retrieval_handlers", article_handlers)

    with pytest.raises(SystemExit) as raised:
        processor.main(
            [
                "--once",
                "--queue",
                queue_name,
                "--retriever-origin",
                "http://127.0.0.1:8765",
                "--allow-local-fetch",
            ]
        )
    assert raised.value.code == 1
    client.require_ready.assert_called_once_with(
        FEED_FETCH_CAPABILITY if queue_name == "ingestion" else ARTICLE_FETCH_CAPABILITY
    )
    feed_handlers.assert_not_called()
    article_handlers.assert_not_called()
    runtime.assert_not_called()
    client.__exit__.assert_called_once()
    engine.dispose.assert_called_once()


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


@pytest.mark.parametrize("with_decision", [False, True])
def test_admin_production_entrypoint_keeps_session_and_editorial_logins_separate(
    monkeypatch: pytest.MonkeyPatch,
    with_decision: bool,
) -> None:
    captured: dict[str, object] = {"checked": [], "disposed": []}

    class FakeConnection:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeEngine:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        def connect(self) -> FakeConnection:
            return FakeConnection(self.kind)

        def dispose(self) -> None:
            cast(list[str], captured["disposed"]).append(self.kind)

    class FakeAdminSettings:
        public_origin = "https://public.example"
        decision_actor = "site operator"

        def auth_config(self) -> str:
            return "synthetic-auth-config"

    def fake_engine(settings: DatabaseSettings, *, search_path: str) -> Engine:
        assert search_path == "pg_catalog"
        return cast(Engine, FakeEngine(settings.application_name))

    def check_session(connection: FakeConnection) -> None:
        cast(list[str], captured["checked"]).append(connection.kind)

    def check_editorial(connection: FakeConnection, role: str) -> None:
        assert role == "editorial_test"
        cast(list[str], captured["checked"]).append(connection.kind)

    def check_decision(connection: FakeConnection, role: str) -> None:
        assert role == "decision_test"
        cast(list[str], captured["checked"]).append(connection.kind)

    def fake_admin_app(_settings: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    def fake_auth_service(*_args: object) -> str:
        return "auth-service"

    def fake_editorial_reader(engine: Engine, **_kwargs: object) -> tuple[str, Engine]:
        return "reader", engine

    def fake_decision_writer(engine: Engine, **_kwargs: object) -> tuple[str, Engine]:
        return "writer", engine

    def fake_run(app: object, **_kwargs: object) -> None:
        captured["app"] = app

    monkeypatch.setenv("PRIMARY_SIGNAL_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_DATABASE_URL", "postgresql+psycopg://session@db.public.example/app"
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE", "session_test")
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_ADMIN_EDITORIAL_DATABASE_URL",
        "postgresql+psycopg://editorial@db.public.example/app",
    )
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_EDITORIAL_DATABASE_EXPECTED_ROLE", "editorial_test")
    if with_decision:
        monkeypatch.setenv(
            "PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL",
            "postgresql+psycopg://decision@db.public.example/app",
        )
        monkeypatch.setenv(
            "PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_EXPECTED_ROLE", "decision_test"
        )
    monkeypatch.setattr(web, "create_database_engine", fake_engine)
    monkeypatch.setattr(web, "assert_admin_session_database_role", check_session)
    monkeypatch.setattr(web, "assert_editorial_database_role", check_editorial)
    monkeypatch.setattr(web, "assert_publication_decision_role", check_decision)
    monkeypatch.setattr(web, "PublicationDecisionWriter", fake_decision_writer)
    monkeypatch.setattr(web, "AdminAuthSettings", FakeAdminSettings)
    monkeypatch.setattr(web, "AdminAuthService", fake_auth_service)
    monkeypatch.setattr(web, "EditorialReader", fake_editorial_reader)
    monkeypatch.setattr(web, "create_admin_app", fake_admin_app)
    monkeypatch.setattr(web.uvicorn, "run", fake_run)

    web.main(["--surface", "admin"])

    expected = [
        "primary_signal_admin_session",
        "primary_signal_admin_editorial",
    ]
    if with_decision:
        expected.append("primary_signal_admin_decision")
    assert captured["checked"] == expected
    assert captured["disposed"] == expected
    assert captured["auth_service"] == "auth-service"
    assert captured["editorial_reader"][0] == "reader"  # type: ignore[index]
    assert captured["public_origin"] == "https://public.example"
    if with_decision:
        assert captured["decision_writer"][0] == "writer"  # type: ignore[index]
    else:
        assert captured["decision_writer"] is None


def test_admin_decision_login_requires_both_private_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_EXPECTED_ROLE", raising=False)
    assert web.admin_decision_database_settings() is None
    monkeypatch.setenv(
        "PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL",
        "postgresql+psycopg://decision@db.public.example/app",
    )
    with pytest.raises(RuntimeError, match="must be set together"):
        web.admin_decision_database_settings()


def test_admin_decision_login_accepts_url_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file_path = tmp_path / "database-url"
    file_path.write_text("postgresql+psycopg://decision@db.public.example/app\n")
    monkeypatch.delenv("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL_FILE", str(file_path))
    monkeypatch.setenv("PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_EXPECTED_ROLE", "decision_test")

    settings = web.admin_decision_database_settings()

    assert settings is not None
    assert settings.expected_role == "decision_test"
    assert settings.application_name == "primary_signal_admin_decision"

    monkeypatch.setenv(
        "PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_URL",
        "postgresql+psycopg://other@db.public.example/app",
    )
    with pytest.raises(ValueError, match="cannot both be set"):
        web.admin_decision_database_settings()


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
