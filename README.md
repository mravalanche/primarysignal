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

The pre-commit hook runs the offline gate and secret scan. Before pushing, run
`uv run poe check` to add the installed-dependency audit and package build.

## Security

Please report security issues privately. See [SECURITY.md](SECURITY.md).

## Licence

Primary Signal is licensed under the [GNU Affero General Public License,
version 3 or later](LICENSE) (`AGPL-3.0-or-later`).
