from collections.abc import Callable, Sequence

import pytest

from primary_signal.entrypoints import migrate, processor, retriever, scheduler, web

Entrypoint = Callable[[Sequence[str] | None], None]


@pytest.mark.parametrize(
    ("entrypoint", "process_name"),
    [
        (processor.main, "processor"),
        (retriever.main, "retriever"),
        (scheduler.main, "scheduler"),
    ],
)
def test_unimplemented_entrypoints_fail_clearly(
    entrypoint: Entrypoint,
    process_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        entrypoint([])

    assert raised.value.code == 2
    assert capsys.readouterr().err == (
        f"primary-signal-{process_name}: not implemented in the platform scaffold\n"
    )


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


def test_migrate_entrypoint_upgrades_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_upgrade(config: object, revision: str) -> None:
        captured.update(config=config, revision=revision)

    monkeypatch.setattr(migrate.command, "upgrade", fake_upgrade)

    migrate.main([])

    assert captured["revision"] == "head"
    assert migrate.migration_config_path().is_file()
