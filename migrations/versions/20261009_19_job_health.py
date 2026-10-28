"""Grant metadata-only read access for job lifecycle health."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_19"
down_revision: str | Sequence[str] | None = "20261009_18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA primary_signal TO primary_signal_cap_job_health")
    op.execute(
        "GRANT SELECT (id, queue, job_type, status, attempt_count, max_attempts, run_after, "
        "lease_expires_at, heartbeat_at, created_at, completed_at) "
        "ON primary_signal.jobs TO primary_signal_cap_job_health"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (id, queue, job_type, status, attempt_count, max_attempts, run_after, "
        "lease_expires_at, heartbeat_at, created_at, completed_at) "
        "ON primary_signal.jobs FROM primary_signal_cap_job_health"
    )
    op.execute("REVOKE USAGE ON SCHEMA primary_signal FROM primary_signal_cap_job_health")
