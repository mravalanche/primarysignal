"""Harden job lease fencing and bounded machine fields."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_02"
down_revision: str | Sequence[str] | None = "20261001_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add per-claim fencing and tighten queue invariants."""

    op.execute(
        "ALTER TABLE primary_signal.job_attempts "
        "RENAME COLUMN lease_expires_at TO initial_lease_expires_at"
    )
    op.execute("ALTER TABLE primary_signal.jobs ADD COLUMN lease_token uuid")

    op.execute(
        "ALTER TABLE primary_signal.jobs "
        "ADD CONSTRAINT ck_jobs_bounded_max_attempts "
        "CHECK (max_attempts BETWEEN 1 AND 20), "
        "ADD CONSTRAINT ck_jobs_lease_lifecycle CHECK ("
        "(status='running' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
        "(status<>'running' AND worker_id IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)), "
        "ADD CONSTRAINT ck_jobs_valid_job_type "
        "CHECK (job_type ~ '^[a-z][a-z0-9_.-]*$' AND char_length(job_type)<=64), "
        "ADD CONSTRAINT ck_jobs_valid_queue "
        "CHECK (queue ~ '^[a-z][a-z0-9_.-]*$' AND char_length(queue)<=64), "
        "ADD CONSTRAINT ck_jobs_bounded_deduplication_key "
        "CHECK (deduplication_key IS NULL OR char_length(deduplication_key) BETWEEN 1 AND 256), "
        "ADD CONSTRAINT ck_jobs_bounded_worker_id "
        "CHECK (worker_id IS NULL OR char_length(worker_id) BETWEEN 1 AND 128), "
        "ADD CONSTRAINT ck_jobs_valid_last_error_code "
        "CHECK (last_error_code IS NULL OR (last_error_code ~ '^[a-z][a-z0-9_.-]*$' "
        "AND char_length(last_error_code)<=64))"
    )
    op.execute(
        "ALTER TABLE primary_signal.job_attempts "
        "ADD CONSTRAINT ck_job_attempts_bounded_worker_id "
        "CHECK (char_length(worker_id) BETWEEN 1 AND 128), "
        "ADD CONSTRAINT ck_job_attempts_valid_error_code "
        "CHECK (error_code IS NULL OR (error_code ~ '^[a-z][a-z0-9_.-]*$' "
        "AND char_length(error_code)<=64))"
    )


def downgrade() -> None:
    """Remove lease fencing while preserving compatible queue rows."""

    op.execute(
        "ALTER TABLE primary_signal.job_attempts "
        "DROP CONSTRAINT ck_job_attempts_valid_error_code, "
        "DROP CONSTRAINT ck_job_attempts_bounded_worker_id"
    )
    op.execute(
        "ALTER TABLE primary_signal.jobs "
        "DROP CONSTRAINT ck_jobs_valid_last_error_code, "
        "DROP CONSTRAINT ck_jobs_bounded_worker_id, "
        "DROP CONSTRAINT ck_jobs_bounded_deduplication_key, "
        "DROP CONSTRAINT ck_jobs_valid_queue, "
        "DROP CONSTRAINT ck_jobs_valid_job_type, "
        "DROP CONSTRAINT ck_jobs_lease_lifecycle, "
        "DROP CONSTRAINT ck_jobs_bounded_max_attempts"
    )
    op.execute("ALTER TABLE primary_signal.jobs DROP COLUMN lease_token")
    op.execute(
        "ALTER TABLE primary_signal.job_attempts "
        "RENAME COLUMN initial_lease_expires_at TO lease_expires_at"
    )
