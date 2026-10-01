-- Fixed capability roles for the PostgreSQL-backed job queue.
--
-- Login roles, passwords and membership grants are deployment-owned. This file
-- deliberately creates and hardens only non-login capability roles.

DO $primary_signal_roles$
DECLARE
    capability_name text;
    capability_oid oid;
BEGIN
    FOREACH capability_name IN ARRAY ARRAY[
        'primary_signal_cap_queue_submit',
        'primary_signal_cap_queue_consume'
    ]
    LOOP
        SELECT oid INTO capability_oid
        FROM pg_catalog.pg_roles
        WHERE rolname = capability_name;

        IF capability_oid IS NULL THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOINHERIT NOREPLICATION NOBYPASSRLS',
                capability_name
            );
        ELSE
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE oid = capability_oid
                AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                     OR rolinherit OR rolreplication OR rolbypassrls)
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_auth_members
                WHERE roleid = capability_oid OR member = capability_oid
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_shdepend
                WHERE refclassid = 'pg_catalog.pg_authid'::regclass
                AND refobjid = capability_oid
                AND deptype IN ('a', 'o')
            ) THEN
                RAISE EXCEPTION
                    'existing role % is not an unused Primary Signal capability role',
                    capability_name;
            END IF;
        END IF;
    END LOOP;
END
$primary_signal_roles$;
