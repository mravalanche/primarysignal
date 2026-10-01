"""Grant the feed scheduler only the columns needed to dispatch polls."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_04"
down_revision: str | Sequence[str] | None = "20261001_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Grant narrow read and scheduling-update access."""

    op.execute("GRANT USAGE ON SCHEMA primary_signal TO primary_signal_cap_feed_schedule")
    op.execute(
        "GRANT SELECT (id, source_id, enabled, poll_interval_seconds, next_poll_at) "
        "ON primary_signal.feeds TO primary_signal_cap_feed_schedule"
    )
    op.execute(
        "GRANT UPDATE (next_poll_at, updated_at) ON primary_signal.feeds "
        "TO primary_signal_cap_feed_schedule"
    )
    op.execute(
        "GRANT SELECT (id, enabled) ON primary_signal.sources TO primary_signal_cap_feed_schedule"
    )


def downgrade() -> None:
    """Remove only the feed-scheduling capability grants."""

    op.execute(
        "REVOKE SELECT (id, enabled) ON primary_signal.sources "
        "FROM primary_signal_cap_feed_schedule"
    )
    op.execute(
        "REVOKE UPDATE (next_poll_at, updated_at) ON primary_signal.feeds "
        "FROM primary_signal_cap_feed_schedule"
    )
    op.execute(
        "REVOKE SELECT (id, source_id, enabled, poll_interval_seconds, next_poll_at) "
        "ON primary_signal.feeds FROM primary_signal_cap_feed_schedule"
    )
    op.execute("REVOKE USAGE ON SCHEMA primary_signal FROM primary_signal_cap_feed_schedule")
