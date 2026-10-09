"""Store opaque admin-session digests and bounded login attempts."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_13"
down_revision: str | Sequence[str] | None = "20261009_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_admin_session"


def upgrade() -> None:
    op.execute("""
CREATE TABLE primary_signal.admin_sessions (
    digest bytea PRIMARY KEY CHECK (octet_length(digest) = 32),
    csrf_secret bytea NOT NULL CHECK (octet_length(csrf_secret) = 32),
    created_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (last_seen_at >= created_at)
)
""")
    op.execute("""
CREATE TABLE primary_signal.admin_login_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_digest bytea NOT NULL CHECK (octet_length(source_digest) = 32),
    occurred_at timestamptz NOT NULL
)
""")
    op.execute(
        "CREATE INDEX ix_admin_login_attempts_recent ON primary_signal.admin_login_attempts(occurred_at)"
    )
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    op.execute(f"GRANT SELECT,INSERT,UPDATE ON primary_signal.admin_sessions TO {ROLE}")
    op.execute(f"GRANT SELECT,INSERT ON primary_signal.admin_login_attempts TO {ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE primary_signal.admin_login_attempts_id_seq TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE USAGE ON SEQUENCE primary_signal.admin_login_attempts_id_seq FROM {ROLE}")
    op.execute(f"REVOKE SELECT,INSERT ON primary_signal.admin_login_attempts FROM {ROLE}")
    op.execute(f"REVOKE SELECT,INSERT,UPDATE ON primary_signal.admin_sessions FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
    op.execute("DROP TABLE primary_signal.admin_login_attempts")
    op.execute("DROP TABLE primary_signal.admin_sessions")
