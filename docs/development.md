# Local development

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

To try the synthetic reading desk on the ordinary public routes locally:

```sh
uv run primary-signal-web --surface public --demo
```

Open <http://127.0.0.1:8000/>. The older
<http://127.0.0.1:8000/__dev/preview> development route is also available.
Both use invented content and reserved example domains. The `--demo` mode and
development preview route are unavailable in production.

The Docker demonstration also fills the ordinary public reading pages with
fictional stories. It runs without a database, live feeds, or a model:

```sh
docker compose -f compose.preview.yaml up --build --wait
# Open http://127.0.0.1:8000/ for Latest, story pages and tags
# Open http://127.0.0.1:8000/progress for the delivery ledger
docker compose -f compose.preview.yaml down
```

Set `PRIMARY_SIGNAL_PREVIEW_PORT` to use a different loopback port. This
Compose file runs only the synthetic public demonstration. It has no database login
or production data. `scripts/smoke_container_preview.sh` checks the image, page,
and stylesheet in a disposable Compose project. On a VM with a root-owned Docker
socket, run `sudo bash scripts/smoke_container_preview.sh`.

The completed product is intended to run as Docker containers on a separate
host. The production Compose stack and host egress policy remain separate
delivery work; this preview file is not a production deployment definition.

The next publication milestone has a repeatable synthetic draft seed. Run
`primary-signal-sample-seed` only against a disposable database ending in
`_test`, after its migrations and test roles are installed. It requires
`PRIMARY_SIGNAL_TEST_DATABASE_URL` and `PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE`
for provenance records, plus `PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_URL` and
`PRIMARY_SIGNAL_TEST_PUBLICATION_DATABASE_EXPECTED_ROLE` for the restricted
writer. Both logins must reach the same database and must be distinct. The
command creates one fictional source, article, fetch, content version and draft;
rerunning it verifies the existing lineage without duplicating the draft. The
full disposable gate in `scripts/dev_test_env.sh` exercises this path.

The ledger at `/progress` calculates each percentage from versioned exit checks
in `src/primary_signal/web/public/milestones.json`. At every substantive commit,
review the checks against evidence, update their state and next action, and run
the progress and container smoke checks. Keep unverified checks open.

The production public surface uses `PostgresStoryReader` and requires a
dedicated PostgreSQL login that inherits only
`primary_signal_cap_public_read`. Configure its URL and exact login name with
`PRIMARY_SIGNAL_DATABASE_URL` and `PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE` in
the public process environment; see `.env.public.example` for placeholder
names. The web process checks that role and its restricted privileges before
accepting requests, then reads only the `primary_signal_public` projection.
For a mounted secret file, set `PRIMARY_SIGNAL_DATABASE_URL_FILE` instead of
`PRIMARY_SIGNAL_DATABASE_URL`. The file must contain one UTF-8 URL line of at
most 4096 bytes; a final newline is allowed. The same `URL_FILE` alternative
works for the admin editorial and publication logins using their respective
`PRIMARY_SIGNAL_ADMIN_EDITORIAL_DATABASE_` and
`PRIMARY_SIGNAL_ADMIN_PUBLICATION_DATABASE_` prefixes. The processor,
scheduler, inventory, source-health, retention-cleanup, and migration commands
use the ordinary `PRIMARY_SIGNAL_DATABASE_` prefix. Keep the expected-role variable
for every login. Never set `URL` and `URL_FILE` together for one prefix.
An administrator must provision the login and grant, and remove its `CREATE`
privilege on PostgreSQL's default
`public` schema. Do not reuse migration or ingestion credentials.

Lifecycle scripts are disabled during installation; the pinned toolchain builds
and tests successfully without them.

The pre-commit hook runs the offline gate and secret scan. Before pushing, run
`uv run poe check` to add Python and frontend advisory audits and the package
build.

## Complete VM test gate

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
