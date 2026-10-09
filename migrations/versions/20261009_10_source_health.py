"""Grant a narrow read-only source and feed health capability."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_10"
down_revision: str | Sequence[str] | None = "20261009_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA primary_signal TO primary_signal_cap_source_health")
    op.execute(
        "GRANT SELECT (id, source_key, name, enabled) ON primary_signal.sources TO primary_signal_cap_source_health"
    )
    op.execute(
        "GRANT SELECT (id, source_id, name, enabled, poll_interval_seconds, "
        "next_poll_at, last_attempt_at, last_success_at, consecutive_failures) "
        "ON primary_signal.feeds TO primary_signal_cap_source_health"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (id, source_id, name, enabled, poll_interval_seconds, "
        "next_poll_at, last_attempt_at, last_success_at, consecutive_failures) "
        "ON primary_signal.feeds FROM primary_signal_cap_source_health"
    )
    op.execute(
        "REVOKE SELECT (id, source_key, name, enabled) ON primary_signal.sources FROM primary_signal_cap_source_health"
    )
    op.execute("REVOKE USAGE ON SCHEMA primary_signal FROM primary_signal_cap_source_health")
