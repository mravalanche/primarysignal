# Disposable publication stack

`compose.publication.yaml` builds locally. `compose.publication.image.yaml`
pulls one published development image for migration, seed, public and admin.
Both are disposable M2 exercises. They use a separate
PostgreSQL volume and a database whose name must end in `_test`. The public
HTTP and admin HTTPS ports bind to host loopback only. The database has no
published port. Public and admin use separate internal database networks, with
PostgreSQL attached to both. Each web namespace also has its own ingress bridge
for loopback port publishing; the public and admin surfaces share no network.
Migration checks the connected database name before changing its schema.

These disposable ingress bridges permit web egress. Production egress policy
and deployment network probes remain release work; this exercise does not
establish production network isolation.

## Synthetic browser smoke

For a repeatable local check, use the smoke runner with Docker, OpenSSL, the
pinned Node toolchain and Playwright Chromium available:

```sh
bash scripts/dev_test_env.sh --skip-apt --setup-only
uv run poe publication-smoke
```

The runner builds the current application image and uses the Compose services
below with temporary synthetic configuration. It creates a unique project and
volume, a private Docker configuration, fixed test-only accounts, and an
ephemeral localhost TLS fixture. Operator environment and secret files belong
to the manual setup below; the smoke uses its own temporary files.

Before startup, it checks the rendered model's paths, loopback ports, internal
networks, service image and database roles. The browser then signs in through
HTTPS, publishes the seeded draft through the Reading desk, and reads the
public story and source trail. It restarts the running services with the same
volume, checks publication persisted, suppresses through the browser, and
restarts again to check the story remains absent publicly.

The browser is restricted to the two local fixture origins. Its HTTPS exception
applies only to that test context; authentication and Secure cookies retain
their application settings. Temporary configuration, TLS material, browser
state, containers and the project volume are removed after the run. The
summary reports checks rather than environment values or database contents.

## Verified milestone evidence

On 2026-10-28, `uv run --locked --offline poe publication-smoke` passed
against the reviewed M2 source changes based on `cafce41`. The local image
content ID was
`sha256:35da4cc8089bb37d4b52e0d1b00dcb73f5c0982c9b3a5a909c0bd8bddc0cec28`.
The runner removed this image after verification.

The run proved clean startup, restricted migration to schema head
`20261028_20`, real HTTPS login with the browser's natural Origin, HTML
publication, public provenance, and suppression through the existing dialog.
Publication and suppression survived separate restarts of the same runtime
containers and volume. Migration and seed did not rerun. The final audit
history retained draft, validation, publication and suppression events. All
owned containers, networks, the volume and temporary fixture files were
removed. This closes M2; production deployment and egress verification remain
open.

## Operator setup

Copy each `.env.publication.*.example` file to the corresponding ignored file
without `.example`. Replace every `REPLACE_ME` with operator-owned local
values. Set `POSTGRES_DB` to a disposable name ending in `_test` and keep that
name identical in every database URL. In the official PostgreSQL image,
`POSTGRES_USER` becomes the bootstrap superuser. Set it to a distinct bootstrap
owner such as `bootstrap_owner_test` and place that password in the ignored
file `.secrets/publication/database_password`. The bootstrap login must not
appear in migration, seed or web environment files. Production needs a
separate bootstrap owner as well. Do not commit these files.

Provide `.secrets/publication/local_logins.sql` before first database start.
It must create a separate `migration_test` login with the database CREATE and
role membership needed to run the repository migrations and transfer the
public projection to `primary_signal_public_owner`. It must also create five
distinct restricted runtime logins and grant each exactly one
capability: `public_reader_test` → `primary_signal_cap_public_read`,
`publication_writer_test` → `primary_signal_cap_publication_write`,
`admin_session_test` → `primary_signal_cap_admin_session`,
`editorial_reader_test` → `primary_signal_cap_editorial_read`, and
`publication_decision_test` → `primary_signal_cap_publication_decision`.
Give them `LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
NOBYPASSRLS` and remove `CREATE` on the default `public` schema. The
PostgreSQL image runs the repository's non-login capability scripts first,
then this operator-owned login script. The migration login owns the private
schema; the bootstrap superuser is kept out of all app services. No migration
or publication-writer credential is present in either web service. Never use
the fixed login values from the test-role helper here.

The bootstrap scripts and login SQL are mounted as separate read-only files.
Keep `.secrets/publication` private to its owner. Its bound login SQL must be
readable by the PostgreSQL container user, and its TLS files by the TLS sidecar
user. Local Compose preserves host file permissions for these binds. Read-only
leaves beneath a `0700` parent directory retain host privacy while allowing the
intended containers to read their explicit mounts.

Provide an operator-owned Argon2id PHC password hash and a hex session key
of at least 32 bytes through `.env.publication.admin`. Keep the PHC value
single-quoted so Compose preserves its dollar signs. Keep the exact admin
origin at `https://localhost:8443`. Provide a locally trusted TLS certificate
covering `localhost` and its key at
`.secrets/publication/admin_tls_certificate.pem` and
`.secrets/publication/admin_tls_key.pem`. The admin process binds only to
container loopback. The TLS sidecar shares that network namespace and does
not send forwarding headers to the app.

## Run and review

After the operator files exist on the development VM, validate the local-build
model with
`docker compose -f compose.publication.yaml config --quiet`, then start with
`docker compose -f compose.publication.yaml up --build --wait`. The migration
and seed services run once; the seed creates a fictional draft with source,
article, fetch and content-version lineage. Browse the admin origin, publish
the draft, verify it at `http://127.0.0.1:8002`, suppress it in admin, then
verify it disappears. Restart the stack and check the state again. The
synthetic smoke above provides the automated version of this exercise.

Stop with `docker compose -f compose.publication.yaml down`. The named volume
persists for restart checks. `down --volumes` destroys the disposable database
and should only be used when its contents are no longer needed.

## Run from a published development image on another Docker host

On the Docker host, check that `docker info --format '{{.Architecture}}'`
reports `x86_64` or `amd64`. The current development image is built for Linux
amd64. Use a `develop` revision containing the Compose bundle and select the
image digest from that revision's successful Publish development image job.

The bundle needs `compose.publication.image.yaml`, the five
`.env.publication.*.example` templates, `deploy/postgres/initdb/`, and
`deploy/publication/admin-nginx.conf`; a repository checkout supplies all of
them. The image contains application code, assets and migrations, so no build
context is needed on this host.

Create the ignored `.env.publication.*` and `.secrets/publication/*` files on
that Docker host as described above. Keep the admin origin at
`https://localhost:8443` and use a certificate valid for `localhost`. The
repository owner controls GHCR package visibility. If the image is private,
authenticate Docker to GHCR with operator-owned credentials on that host.
Do not put registry credentials in Compose or the repository.

The successful Publish development image job's GitHub Actions run summary
contains a full `ghcr.io/mravalanche/primarysignal@sha256:...` reference.
Set that reference in the shell, then validate, pull and start the stack:

```sh
export PRIMARY_SIGNAL_PUBLICATION_IMAGE='ghcr.io/mravalanche/primarysignal@sha256:REPLACE_WITH_PUBLISHED_DIGEST'
docker compose -f compose.publication.image.yaml config --quiet
docker compose -f compose.publication.image.yaml pull
docker compose -f compose.publication.image.yaml up --wait
```

The public and admin ports bind only to the Docker host's loopback. From a
different computer, use an SSH tunnel to the Docker host before browsing:

```sh
ssh -N -L 8002:127.0.0.1:8002 -L 8443:127.0.0.1:8443 DOCKER_HOST
```

Browse `http://localhost:8002` and `https://localhost:8443` through the
tunnel. Run the same publish, read, suppress and restart checks described
above. Stop with `docker compose -f compose.publication.image.yaml down`.
The named volume persists. Do not expose these ports through a public
reverse proxy; this is a disposable development stack, not a release.
