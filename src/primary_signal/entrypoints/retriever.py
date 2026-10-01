"""Run the isolated retrieval service."""

from collections.abc import Sequence

from primary_signal.entrypoints._unimplemented import fail_unimplemented


def main(argv: Sequence[str] | None = None) -> None:
    """Report that retrieval is not available in this scaffold."""

    del argv
    fail_unimplemented("retriever")


if __name__ == "__main__":  # pragma: no cover - exercised through console scripts
    main()
