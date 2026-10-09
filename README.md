# Primary Signal

Primary Signal is a self-hosted cyber-security briefing and publication. It
groups reporting about the same development, ranks stories using clear factors,
and keeps source evidence close to every summary.

The project is under development. The first release will focus on:

- curated RSS and Atom ingestion;
- reliable article retrieval and provenance;
- conservative story clustering;
- explainable ranking and evidence-backed status;
- a responsive public publication and separate local administration surface.

See the [product contract](docs/product-contract.md), [delivery
plan](docs/PLAN.md), and [architecture decisions](docs/adr/README.md) for the
current direction.

## Development

Primary Signal uses Python 3.14, [uv](https://docs.astral.sh/uv/) for the
environment and lockfile, and Poe for project commands.

```sh
uv sync --locked --all-groups
uv run pre-commit install
uv run poe check-offline
```

The server-rendered interface uses Jinja, HTMX, Tailwind CSS and a constrained
DaisyUI component subset. Node 24 Active LTS is only used to build and test
self-hosted assets. After cloning, build them with:

```sh
npm ci --ignore-scripts
uv run poe assets-build
```

To try the synthetic news-desk preview locally:

```sh
uv run primary-signal-web --surface public
```

Open <http://127.0.0.1:8000/__dev/preview>. The preview uses invented content
and reserved example domains. It is not registered in production.

The production public surface uses `PostgresStoryReader` and requires a
dedicated PostgreSQL login that inherits only
`primary_signal_cap_public_read`. Configure its URL and exact login name with
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE` in
the public process environment; see `.env.public.example` for placeholder
names. The web process checks that role and its restricted privileges before
accepting requests, then reads only the `primary_signal_public` projection.
An administrator must provision the login and grant, and remove its `CREATE`
privilege on PostgreSQL's default
`public` schema. Do not reuse migration or ingestion credentials.

Lifecycle scripts are disabled during installation; the pinned toolchain builds
and tests successfully without them.

The pre-commit hook runs the offline gate and secret scan. Before pushing, run
`uv run poe check` to add Python and frontend advisory audits and the package
build.

### Complete VM test gate

On an Ubuntu 26.04 development VM, run `scripts/dev_test_env.sh` from a source
checkout. It installs PostgreSQL 18 and Python's 3.14 virtual-environment
package if needed, bootstraps uv 0.12.21 if absent, downloads the exact Node
version in `.node-version` into a private user cache, syncs locked Python and
npm dependencies, and runs the full `poe check` gate. The script needs `sudo`
only for missing apt packages.

```sh
bash scripts/dev_test_env.sh
```

The gate starts a fresh PostgreSQL cluster in a temporary directory with TCP
disabled and an owner-only Unix socket. It creates the synthetic capability
roles, applies migrations, runs all tests including PostgreSQL privilege tests,
then stops and removes that cluster even if a check fails. No existing database
or Compose volume is touched. The cluster uses local socket trust authentication;
only its owning VM user can reach the socket. Use `--offline` for the deterministic
gate, `--setup-only` to install tools without running tests, or `--skip-apt` when
the system packages are already installed.

The tests take their database endpoints from the `PRIMARY_SIGNAL_TEST_*_DATABASE_URL`
settings exported by this script. That keeps a future Docker-backed test runner
limited to provisioning a disposable database and supplying the same settings;
Docker and daemon permissions are not needed for this VM gate.

### Feed scheduler

The scheduler queues due feed polls and does no network work itself. It runs
continuously by default, or performs one pass for operational checks:

```sh
uv run primary-signal-scheduler
uv run primary-signal-scheduler --once
```

It uses the `PRIMARY_SIGNAL_DATABASE_*` connection settings and its own
`PRIMARY_SIGNAL_SCHEDULER_*` tuning settings. The defaults are a batch of 100,
a 30-second idle poll interval, and equal-jitter database retries starting
between half a second and one second, capped at 60 seconds. The database login
must hold the queue-submit and feed-scheduling capability roles described below.
`SIGINT` and `SIGTERM`
stop idle waits immediately and let an in-flight bounded database operation
finish before the process exits. A statement timeout applies to each statement,
not the whole batch, so production wiring also needs a whole-pass time budget
and shutdown grace that covers it.

### Feed processor foundation

The ingestion processor now has lease renewal, expired-job recovery, bounded
database retries, and atomic completion callbacks. Feed parsing and poll-record
persistence are available for synthetic tests and an injected fetcher. The
processor command intentionally refuses to start without a bound feed handler.
Live polling depends on the isolated retriever and its network controls; the
processor must not fetch internet URLs directly.

### Feed and article retrieval foundation

The retriever library validates feed URLs and every DNS answer against a dated
special-purpose address policy. Its HTTP transport connects to a validated
address, checks the peer, preserves hostname-based TLS verification, rechecks
redirects, and limits response headers and body size. Synthetic tests exercise
these rules. A fixed feed-fetch API is available through
`primary-signal-retriever --allow-local-fetch` on loopback when
`PRIMARY_SIGNAL_ENVIRONMENT` is explicitly `development` or `test`. Its processor
client uses a fixed configured origin with no ambient proxy or redirects and a
bounded response. The feed handler can be bound for controlled integration
tests; normal processor startup still fails closed, and the retriever command
rejects production startup until deployment network controls are ready.
DNS callers now have a finite wait and a shared limit on simultaneous system
lookups. A stuck platform lookup cannot be cancelled and holds its capacity
slot until it exits. The network egress controls described in ADR 0007 remain
required before live polling can be enabled.

The local retriever also has a fixed article-fetch endpoint. It applies the
same address and redirect policy, extracts bounded plain text from HTML inside
the database-free retriever, and returns content hashes and redirect history.
Raw HTML is discarded there. An explicit, injectable article job handler now
records fetch attempts and immutable extracted-text versions in PostgreSQL.
Repeated content reuses the same version. Live article retrieval remains
disabled until deployment egress controls, a retention rule, and source-disable
serialization are settled.

### Internal article inventory

The article inventory reads only each article's current extracted version. It
uses PostgreSQL English-language full-text search and returns source, title,
date, and word-count metadata. From a local terminal with a dedicated inventory
database login configured through `PRIMARY_SIGNAL_DATABASE_URL` and
`PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`, run:

```sh
uv run primary-signal-article-inventory --search "synthetic notice" --limit 20
```

The JSON output includes an opaque cursor for the next page, but no extracted
body or raw URL. This is an internal operator command, not a public story
search endpoint. The public and administration sites do not expose this reader.

### Source and feed health

From a local terminal, use a dedicated database login with only the
`primary_signal_cap_source_health` capability. Set
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE`
for that login, then run:

```sh
uv run primary-signal-source-health --limit 100
```

The JSON report lists at most the requested number of sources and feeds each,
ordered by source key and then feed name and ID. Truncation flags show when more
rows exist. Feed status is `disabled` when the feed or its source is disabled;
`stale` when it has failures, has never succeeded and is due, or its last
success is more than two poll intervals old; and `healthy` otherwise. The
report contains names and polling timestamps, but no configured URLs, error
details, job payloads, or article content. It is a local operator command.

### Database capability roles

Fresh Compose database volumes create two fixed, non-login queue capabilities,
one feed-scheduling capability, one feed-poll capability, and one capability for
article persistence, a metadata-only inventory search capability, and public
projection owner/read capabilities, a publication-writer capability, and a
source-health read capability. The definitions
live in the numbered SQL files under `deploy/postgres/initdb`. Login roles,
passwords and role membership remain deployment-owned. PostgreSQL only runs
these files while creating a new data directory.

For an existing development volume, apply the role bootstrap from inside the
database container. The command uses the container's existing environment and
does not put a password on the command line:

```sh
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/010_queue_capability_roles.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/020_feed_scheduler_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/030_feed_poll_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/040_article_persist_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/050_article_inventory_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/060_public_projection_roles.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/070_publication_writer_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/080_source_health_capability_role.sql'
```

Run all eight commands before applying migrations. The migrations grant access to
the exact queue and feed columns each capability needs; they do not create login
roles or grant role membership. Each bootstrap fails closed if its cluster-wide
role name is already in use, owns objects, has direct access, or has any
memberships. Apply them before granting capabilities to local runtime logins or
running the grant migrations. A later rerun intentionally fails once grants or
memberships exist.

The publication writer is a manual, trusted-backend foundation. A deployment
administrator must create a separate writer login and grant it only
`primary_signal_cap_publication_write`. Do not use a migration or admin login
as the writer, and do not attach this capability to either web runtime. The
writer API records operator decisions, but direct SQL using that capability
can bypass its audit and eligibility checks. The API has no authentication;
future authenticated service code must bind the recorded actor to its session.
Automatic publication requires the product contract's remaining evidence,
safety, membership, and hold gates before it can be enabled.

## Security

Please report security issues privately. See [SECURITY.md](SECURITY.md).

## Licence

Primary Signal is licensed under the [GNU Affero General Public License,
version 3 or later](LICENSE) (`AGPL-3.0-or-later`).
