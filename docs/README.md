# Documentation

Start with the [README](../README.md) for the synthetic reading desk. These
pages hold the detail behind the project overview.

| Area | Pages |
| --- | --- |
| Product and delivery | [Product contract](product-contract.md) · [Delivery plan](PLAN.md) |
| Development | [Local setup and tests](development.md) · [Development image](development-image.md) · [Disposable publication](disposable-publication.md) |
| Data pipeline | [Feed and article runtime](ingestion-runtime.md) · [Isolated retrieval proof](isolated-retrieval.md) · [Database roles](database-roles.md) |
| Interface | [Design dossier](../DESIGN.md) · [Design system](design-system.md) · [Admin reading desk](admin-reading-desk.md) |
| Operations and security | [Admin authentication](admin-auth.md) · [Security policy](../SECURITY.md) |
| Decisions | [Architecture decision records](adr/README.md) |

The database-backed publication exercise is separate from the database-free
Docker preview. Its browser smoke verifies publication state across restarts;
the delivery plan and progress ledger record the remaining exit checks.
