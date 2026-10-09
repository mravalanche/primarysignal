"""Privilege check for the admin publication decision connection."""

from sqlalchemy import Connection, text

from primary_signal.db.engine import assert_database_role

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
        'primary_signal_cap_publication_decision', 'USAGE')
    AND (SELECT count(*) FROM memberships) = 1
    AND NOT EXISTS (
        SELECT 1 FROM memberships
        JOIN pg_catalog.pg_roles AS inherited ON inherited.oid = memberships.role_oid
        WHERE inherited.rolname <> 'primary_signal_cap_publication_decision'
    )
    AND pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'USAGE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'primary_signal_public', 'CREATE')
    AND NOT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')
    AND pg_catalog.has_function_privilege(current_user,
        'primary_signal.publish_reviewed(uuid,uuid,text,uuid,text,text)', 'EXECUTE')
    AND pg_catalog.has_function_privilege(current_user,
        'primary_signal.suppress_reviewed(uuid,uuid,text,text)', 'EXECUTE')
    AND NOT pg_catalog.has_function_privilege(current_user,
        'primary_signal.lock_story_for_draft(text)', 'EXECUTE')
    AND NOT pg_catalog.has_function_privilege(current_user,
        'primary_signal.finalize_draft(uuid,uuid)', 'EXECUTE')
    AND NOT pg_catalog.has_function_privilege(current_user,
        'primary_signal.draft_fingerprint_v2(uuid,uuid)', 'EXECUTE')
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname IN ('primary_signal', 'primary_signal_public')
          AND relation.relkind IN ('r','p','v','m','f')
          AND (pg_catalog.has_table_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege(current_user, relation.oid,
                  'SELECT,INSERT,UPDATE,REFERENCES'))
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS sequence
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = sequence.relnamespace
        WHERE namespace.nspname IN ('primary_signal', 'primary_signal_public')
          AND sequence.relkind = 'S'
          AND pg_catalog.has_sequence_privilege(current_user, sequence.oid,
              'USAGE,SELECT,UPDATE')
    ) AS is_restricted_publication_decision
"""
)


def assert_publication_decision_role(connection: Connection, expected_role: str) -> None:
    """Require an exact login with only the reviewed-transition capability."""

    assert_database_role(connection, expected_role)
    if connection.execute(_ROLE_CHECK).scalar_one() is not True:
        raise RuntimeError("publication decision role lacks restricted privileges")
