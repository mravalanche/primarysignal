"""PostgreSQL session store using only the admin-session capability."""

from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from primary_signal.web.admin.auth import StoredSession


def assert_admin_session_database_role(connection: Connection) -> None:
    """Reject a session login that can read articles or write publication data."""

    permitted = connection.execute(
        text(
            """
WITH RECURSIVE memberships(role_oid) AS (
    SELECT roleid FROM pg_catalog.pg_auth_members
    WHERE member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
    UNION
    SELECT membership.roleid FROM pg_catalog.pg_auth_members AS membership
    JOIN memberships ON membership.member = memberships.role_oid
)
SELECT session_user = current_user
    AND pg_catalog.pg_has_role(current_user, 'primary_signal_cap_admin_session', 'USAGE')
    AND pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'USAGE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal_public', 'CREATE')
    AND pg_catalog.has_table_privilege(current_user, 'primary_signal.admin_sessions', 'SELECT')
    AND pg_catalog.has_table_privilege(current_user, 'primary_signal.admin_sessions', 'INSERT')
    AND pg_catalog.has_table_privilege(current_user, 'primary_signal.admin_sessions', 'UPDATE')
    AND pg_catalog.has_table_privilege(current_user, 'primary_signal.admin_login_attempts', 'SELECT')
    AND pg_catalog.has_table_privilege(current_user, 'primary_signal.admin_login_attempts', 'INSERT')
    AND pg_catalog.has_sequence_privilege(current_user,
                                         'primary_signal.admin_login_attempts_id_seq', 'USAGE')
    AND COALESCE((SELECT NOT (rolsuper OR rolcreatedb OR rolcreaterole OR
                             rolreplication OR rolbypassrls)
                  FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
    AND NOT EXISTS (
        SELECT 1 FROM memberships JOIN pg_catalog.pg_roles AS inherited
        ON inherited.oid = memberships.role_oid
        WHERE inherited.rolname <> 'primary_signal_cap_admin_session'
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname IN ('primary_signal', 'primary_signal_public')
          AND relation.relkind IN ('r','p','v','m','f')
          AND relation.relname NOT IN ('admin_sessions', 'admin_login_attempts')
          AND (pg_catalog.has_table_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,REFERENCES'))
    ) AS permitted
"""
        )
    ).scalar_one()
    if permitted is not True:
        raise RuntimeError("admin session database role lacks restricted privileges")


class PostgresSessionStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def claim_login_attempt(
        self,
        source_digest: bytes,
        *,
        since: datetime,
        at: datetime,
        source_limit: int,
        global_limit: int,
    ) -> bool:
        with self.engine.begin() as connection:
            # Serialize the shared service budget across all app workers.
            connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(1288737043)"))
            row = connection.execute(
                text(
                    "SELECT count(*) AS total, count(*) FILTER (WHERE source_digest=:source) AS source_total "
                    "FROM primary_signal.admin_login_attempts WHERE occurred_at >= :since"
                ),
                {"source": source_digest, "since": since},
            ).one()
            if int(row.total) >= global_limit or int(row.source_total) >= source_limit:
                return False
            connection.execute(
                text(
                    "INSERT INTO primary_signal.admin_login_attempts (source_digest,occurred_at) "
                    "VALUES (:source,:at)"
                ),
                {"source": source_digest, "at": at},
            )
        return True

    def create(self, session: StoredSession) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO primary_signal.admin_sessions "
                    "(digest,csrf_secret,created_at,last_seen_at) VALUES (:digest,:csrf,:created,:seen)"
                ),
                {
                    "digest": session.digest,
                    "csrf": session.csrf_secret,
                    "created": session.created_at,
                    "seen": session.last_seen_at,
                },
            )

    def find_and_touch(
        self, digest: bytes, *, now: datetime, idle_since: datetime, absolute_since: datetime
    ) -> StoredSession | None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                    "UPDATE primary_signal.admin_sessions SET last_seen_at=greatest(last_seen_at,:now) "
                        "WHERE digest=:digest AND revoked_at IS NULL AND last_seen_at > :idle_since "
                        "AND created_at > :absolute_since "
                        "RETURNING digest,csrf_secret,created_at,last_seen_at"
                    ),
                    {
                        "digest": digest,
                        "now": now,
                        "idle_since": idle_since,
                        "absolute_since": absolute_since,
                    },
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return StoredSession(
            digest=bytes(row["digest"]),
            csrf_secret=bytes(row["csrf_secret"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
        )

    def revoke(self, digest: bytes) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE primary_signal.admin_sessions SET revoked_at=now() "
                    "WHERE digest=:digest AND revoked_at IS NULL"
                ),
                {"digest": digest},
            )
