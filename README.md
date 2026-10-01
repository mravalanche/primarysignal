# Primary Signal

Primary Signal is a self-hosted cyber-security briefing and publication. It
groups reporting about the same development, ranks stories using clear factors,
and keeps source evidence close to every summary.

The project is in planning. The first release will focus on:

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

### Database capability roles

Fresh Compose database volumes create two fixed, non-login queue capabilities
and one feed-scheduling capability. The definitions live in the numbered SQL
files under `deploy/postgres/initdb`. Login roles, passwords and role membership
remain deployment-owned. PostgreSQL only runs these files while creating a new
data directory.

For an existing development volume, apply the role bootstrap from inside the
database container. The command uses the container's existing environment and
does not put a password on the command line:

```sh
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/010_queue_capability_roles.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/020_feed_scheduler_capability_role.sql'
```

Run both commands before applying migrations. The migrations grant access to
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
