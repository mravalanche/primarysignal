# Disposable publication stack

`compose.publication.yaml` builds locally. `compose.publication.image.yaml`
pulls one published development image for migration, seed, public and admin.
Both are disposable M2 exercises. They use a separate
PostgreSQL volume and a database whose name must end in `_test`. The public
HTTP and admin HTTPS ports bind to host loopback only. The database has no
published port. Public and admin use separate internal database networks, with
PostgreSQL attached to both. Migration checks the connected database name
before changing its schema. This file is not a production deployment definition.

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

Provide an operator-owned Argon2id PHC password hash and a hex session key
of at least 32 bytes through `.env.publication.admin`. Keep the exact admin
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
automated clean-start browser smoke is still pending.

Stop with `docker compose -f compose.publication.yaml down`. The named volume
persists for restart checks. `down --volumes` destroys the disposable database
and should only be used when its contents are no longer needed.

## Run from a published development image on another Docker host

On the Docker host, check that `docker info --format '{{.Architecture}}'`
reports `x86_64` or `amd64`. The current development image is built for Linux
amd64. While this bundle is in draft PR #54, check out its
`feat/disposable-publication-compose` branch on the Docker host. Choose the
image digest from a successful Publish development image job on `develop`.
The draft branch contains the Compose bundle; the successful `develop` run
contains the published application image. After PR #54 merges, use a `develop`
revision containing the bundle and the digest published for that revision.

For the draft bundle, start with:

```sh
git clone --branch feat/disposable-publication-compose https://github.com/mravalanche/primarysignal.git
cd primarysignal
```

After merge, clone `develop` instead and select its successful publish run.

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
