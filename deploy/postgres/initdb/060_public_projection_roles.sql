-- Fixed non-login roles for the public projection. Login roles and passwords are
-- deployment-owned. Run before Alembic, as with the ingestion capabilities.
DO $primary_signal_public_roles$
DECLARE
    role_name text;
    role_oid oid;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'primary_signal_public_owner', 'primary_signal_cap_public_read'
    ] LOOP
        SELECT oid INTO role_oid FROM pg_catalog.pg_roles WHERE rolname = role_name;
        IF role_oid IS NULL THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOINHERIT NOREPLICATION NOBYPASSRLS', role_name
            );
        ELSIF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles WHERE oid = role_oid
            AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                 OR rolinherit OR rolreplication OR rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members
            WHERE roleid = role_oid OR member = role_oid
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_shdepend
            WHERE refclassid = 'pg_catalog.pg_authid'::regclass
              AND refobjid = role_oid AND deptype IN ('a', 'o')
        ) THEN
            RAISE EXCEPTION 'existing role % is not an unused Primary Signal role', role_name;
        END IF;
    END LOOP;
END
$primary_signal_public_roles$;
