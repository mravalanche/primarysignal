"""Allow a separate admin login to execute only reviewed transitions."""

from collections.abc import Sequence

from alembic import op

revision: str = "20261009_15"
down_revision: str | Sequence[str] | None = "20261009_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "primary_signal_cap_publication_decision"
PUBLISH = "primary_signal.publish_reviewed(uuid,uuid,text,uuid,text,text)"
SUPPRESS = "primary_signal.suppress_reviewed(uuid,uuid,text,text)"


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA primary_signal TO {ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {PUBLISH} TO {ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SUPPRESS} TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SUPPRESS} FROM {ROLE}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {PUBLISH} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA primary_signal FROM {ROLE}")
