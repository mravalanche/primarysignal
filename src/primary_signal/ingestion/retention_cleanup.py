"""Execute the database's fixed-policy, bounded extracted-text cleanup."""

import uuid

from sqlalchemy import Connection, text

_ROLE_CHECK = text(
    """
WITH RECURSIVE memberships(role_oid) AS (
    SELECT membership.roleid FROM pg_catalog.pg_auth_members AS membership
    WHERE membership.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
    UNION
    SELECT membership.roleid FROM pg_catalog.pg_auth_members AS membership
    JOIN memberships ON membership.member = memberships.role_oid
)
SELECT session_user = current_user
    AND COALESCE((SELECT rolcanlogin AND rolinherit
                        AND NOT (rolsuper OR rolcreaterole OR rolcreatedb
                                 OR rolbypassrls OR rolreplication)
                  FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
    AND pg_catalog.pg_has_role(current_user,
        'primary_signal_cap_retention_cleanup', 'USAGE')
    AND (SELECT count(*) FROM memberships) = 1
    AND NOT EXISTS (
        SELECT 1 FROM memberships
        JOIN pg_catalog.pg_roles AS inherited ON inherited.oid = memberships.role_oid
        WHERE inherited.rolname <> 'primary_signal_cap_retention_cleanup'
    )
    AND pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'USAGE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal_public', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')
    AND pg_catalog.has_function_privilege(current_user,
        'primary_signal.clear_expired_extracted_text(integer)', 'EXECUTE')
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname IN ('primary_signal', 'primary_signal_public')
          AND relation.relkind IN ('r','p','v','m','f')
          AND (pg_catalog.has_table_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,REFERENCES'))
    ) AS is_restricted_retention_cleanup
"""
)


def assert_retention_cleanup_role(connection: Connection) -> None:
    """Require a login with this capability and no other database powers."""

    if connection.execute(_ROLE_CHECK).scalar_one() is not True:
        raise RuntimeError("retention cleanup role lacks restricted privileges")


def clear_expired_extracted_text(
    connection: Connection, *, limit: int = 100
) -> tuple[uuid.UUID, ...]:
    """Return cleared version IDs; the database owns all eligibility checks."""

    if not 1 <= limit <= 500:
        raise ValueError("retention cleanup limit must be between 1 and 500")
    assert_retention_cleanup_role(connection)
    rows = connection.execute(
        text("SELECT primary_signal.clear_expired_extracted_text(:limit)"),
        {"limit": limit},
    ).scalars()
    return tuple(rows)
