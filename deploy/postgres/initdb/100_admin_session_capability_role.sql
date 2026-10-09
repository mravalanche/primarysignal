-- Narrow storage capability for private administration sessions.
-- Login roles and membership grants remain deployment-owned.

DO $primary_signal_admin_session_role$
DECLARE
    capability_name CONSTANT text := 'primary_signal_cap_admin_session';
    capability_oid oid;
BEGIN
    SELECT oid INTO capability_oid FROM pg_catalog.pg_roles WHERE rolname = capability_name;
    IF capability_oid IS NULL THEN
        EXECUTE format(
            'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOINHERIT NOREPLICATION NOBYPASSRLS', capability_name
        );
    ELSIF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE oid = capability_oid
        AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolinherit
             OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE roleid = capability_oid OR member = capability_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::regclass
        AND refobjid = capability_oid AND deptype IN ('a', 'o')
    ) THEN
        RAISE EXCEPTION 'existing role % is not an unused Primary Signal capability role',
            capability_name;
    END IF;
END
$primary_signal_admin_session_role$;
