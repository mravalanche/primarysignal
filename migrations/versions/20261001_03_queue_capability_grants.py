"""Restrict queue access to fixed, least-privilege capability roles."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_03"
down_revision: str | Sequence[str] | None = "20261001_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove implicit access and grant only queue operations each role needs."""

    op.execute("REVOKE ALL ON SCHEMA primary_signal FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA primary_signal FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA primary_signal FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA primary_signal FROM PUBLIC")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal REVOKE ALL ON TABLES FROM PUBLIC")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal REVOKE ALL ON SEQUENCES FROM PUBLIC"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA primary_signal REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )

    op.execute(
        "GRANT USAGE ON SCHEMA primary_signal TO "
        "primary_signal_cap_queue_submit, primary_signal_cap_queue_consume"
    )
    op.execute(
        "GRANT INSERT (id, job_type, payload_version, payload, queue, priority, "
        "deduplication_key, status, run_after, max_attempts) "
        "ON primary_signal.jobs TO primary_signal_cap_queue_submit"
    )
    op.execute(
        "GRANT SELECT (id, queue, job_type, deduplication_key, status) "
        "ON primary_signal.jobs TO primary_signal_cap_queue_submit"
    )

    op.execute(
        "GRANT SELECT (id, job_type, payload_version, payload, queue, priority, status, "
        "run_after, attempt_count, max_attempts, worker_id, lease_token, lease_expires_at) "
        "ON primary_signal.jobs TO primary_signal_cap_queue_consume"
    )
    op.execute(
        "GRANT UPDATE (status, run_after, attempt_count, worker_id, lease_token, "
        "lease_expires_at, heartbeat_at, first_started_at, completed_at, "
        "last_error_code, last_error_detail, updated_at) "
        "ON primary_signal.jobs TO primary_signal_cap_queue_consume"
    )
    op.execute(
        "GRANT INSERT (id, job_id, attempt_number, worker_id, status, started_at, "
        "initial_lease_expires_at) ON primary_signal.job_attempts "
        "TO primary_signal_cap_queue_consume"
    )
    op.execute(
        "GRANT SELECT (id, job_id, attempt_number, worker_id, status) "
        "ON primary_signal.job_attempts TO primary_signal_cap_queue_consume"
    )
    op.execute(
        "GRANT UPDATE (status, finished_at, error_code, error_detail) "
        "ON primary_signal.job_attempts TO primary_signal_cap_queue_consume"
    )


def downgrade() -> None:
    """Remove capability grants without restoring unsafe PUBLIC access."""

    op.execute(
        "REVOKE ALL ON primary_signal.job_attempts FROM "
        "primary_signal_cap_queue_submit, primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE ALL ON primary_signal.jobs FROM "
        "primary_signal_cap_queue_submit, primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE INSERT (id, job_type, payload_version, payload, queue, priority, "
        "deduplication_key, status, run_after, max_attempts) "
        "ON primary_signal.jobs FROM primary_signal_cap_queue_submit"
    )
    op.execute(
        "REVOKE SELECT (id, queue, job_type, deduplication_key, status) "
        "ON primary_signal.jobs FROM primary_signal_cap_queue_submit"
    )
    op.execute(
        "REVOKE UPDATE (status, run_after, attempt_count, worker_id, lease_token, "
        "lease_expires_at, heartbeat_at, first_started_at, completed_at, "
        "last_error_code, last_error_detail, updated_at) "
        "ON primary_signal.jobs FROM primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE SELECT (id, job_type, payload_version, payload, queue, priority, status, "
        "run_after, attempt_count, max_attempts, worker_id, lease_token, lease_expires_at) "
        "ON primary_signal.jobs FROM primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE INSERT (id, job_id, attempt_number, worker_id, status, started_at, "
        "initial_lease_expires_at) ON primary_signal.job_attempts "
        "FROM primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE SELECT (id, job_id, attempt_number, worker_id, status) "
        "ON primary_signal.job_attempts FROM primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE UPDATE (status, finished_at, error_code, error_detail) "
        "ON primary_signal.job_attempts FROM primary_signal_cap_queue_consume"
    )
    op.execute(
        "REVOKE ALL ON SCHEMA primary_signal FROM "
        "primary_signal_cap_queue_submit, primary_signal_cap_queue_consume"
    )
