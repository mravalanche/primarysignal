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
        (migrate.main, "migrate"),
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
    ) -> None:
        captured.update(
            app=app,
            host=host,
            port=port,
            proxy_headers=proxy_headers,
        )

    monkeypatch.setattr(web.uvicorn, "run", fake_run)

    web.main(["--surface", "public", "--host", "127.0.0.2", "--port", "8080"])

    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8080
    assert captured["proxy_headers"] is False
    assert captured["app"].state.surface == "public"  # type: ignore[union-attr]
