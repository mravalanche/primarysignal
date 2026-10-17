# Disposable publication stack

`compose.publication.yaml` is a local exercise for M2. It uses a separate
PostgreSQL volume and a database whose name must end in `_test`. The public
HTTP and admin HTTPS ports bind to host loopback only. The database has no
published port. Migration checks the connected database name before changing
its schema. This file is not a production deployment definition.

## Operator setup

Copy each `.env.publication.*.example` file to the corresponding ignored file
without `.example`. Replace every `REPLACE_ME` with operator-owned local
values. Set `POSTGRES_DB` to a disposable name ending in `_test` and keep that
name identical in every database URL. Set `POSTGRES_USER` to the migration
login and place its PostgreSQL password in the ignored file
`.secrets/publication/database_password`. Do not commit these files.

Provide `.secrets/publication/local_logins.sql` before first database start.
It must create five distinct restricted logins and grant each exactly one
capability: `public_reader_test` → `primary_signal_cap_public_read`,
`publication_writer_test` → `primary_signal_cap_publication_write`,
`admin_session_test` → `primary_signal_cap_admin_session`,
`editorial_reader_test` → `primary_signal_cap_editorial_read`, and
`publication_decision_test` → `primary_signal_cap_publication_decision`.
Give them `LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
NOBYPASSRLS` and remove `CREATE` on the default `public` schema. The
PostgreSQL image runs the repository's non-login capability scripts first,
then this operator-owned login script. The migration login owns the schema;
no migration or publication-writer credential is present in either web
service. Never use the fixed login values from the test-role helper here.

Provide an operator-owned Argon2id PHC password hash and a hex session key
of at least 32 bytes through `.env.publication.admin`. Keep the exact admin
origin at `https://localhost:8443`. Provide a locally trusted TLS certificate
covering `localhost` and its key at
`.secrets/publication/admin_tls_certificate.pem` and
`.secrets/publication/admin_tls_key.pem`. The admin process binds only to
container loopback. The TLS sidecar shares that network namespace and does
not send forwarding headers to the app.

## Run and review

After the operator files exist, validate the model with
`docker compose -f compose.publication.yaml config --quiet`, then start with
`docker compose -f compose.publication.yaml up --build --wait`. The migration
and seed services run once; the seed creates a fictional draft with source,
article, fetch and content-version lineage. Browse the admin origin, publish
the draft, verify it at `http://127.0.0.1:8002`, suppress it in admin, then
verify it disappears. Restart the stack and check the state again. The
automated clean-start browser smoke is still pending.

Stop with `docker compose -f compose.publication.yaml down`. The named volume
persists for restart checks. `down --volumes` destroys the disposable database
and should only be used when its contents are no longer needed.
