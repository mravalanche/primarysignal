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

Lifecycle scripts are disabled during installation; the pinned toolchain builds
and tests successfully without them.

The pre-commit hook runs the offline gate and secret scan. Before pushing, run
`uv run poe check` to add Python and frontend advisory audits and the package
build.

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
Raw HTML is discarded there. Article job handling and content-version
persistence are the next step; live article retrieval remains disabled.

### Database capability roles

Fresh Compose database volumes create two fixed, non-login queue capabilities,
one feed-scheduling capability, and one feed-poll capability. The definitions
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
```

Run all three commands before applying migrations. The migrations grant access to
the exact queue and feed columns each capability needs; they do not create login
roles or grant role membership. Each bootstrap fails closed if its cluster-wide
role name is already in use, owns objects, has direct access, or has any
memberships. Apply them before granting capabilities to local runtime logins or
running the grant migrations. A later rerun intentionally fails once grants or
memberships exist.

## Security

Please report security issues privately. See [SECURITY.md](SECURITY.md).

## Licence

Primary Signal is licensed under the [GNU Affero General Public License,
version 3 or later](LICENSE) (`AGPL-3.0-or-later`).
