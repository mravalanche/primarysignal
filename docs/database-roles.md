# Database capability roles

Fresh Compose database volumes create the fixed, non-login capability roles in
the numbered SQL files under `deploy/postgres/initdb`. These include a dedicated
extracted-text cleanup capability. Login roles, passwords and role membership
remain deployment-owned. PostgreSQL only runs these files while creating a new
data directory.

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
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/090_editorial_read_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/100_admin_session_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/110_publication_decision_capability_role.sql'
docker compose exec database sh -c 'psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file /docker-entrypoint-initdb.d/120_retention_cleanup_capability_role.sql'
```

Run all twelve commands before applying migrations. The migrations grant access to
the exact queue and feed columns each capability needs; they do not create login
roles or grant role membership. Each bootstrap fails closed if its cluster-wide
role name is already in use, owns objects, has direct access, or has any
memberships. Apply them before granting capabilities to local runtime logins or
running the grant migrations. A later rerun intentionally fails once grants or
memberships exist.

The publication writer is a manual, trusted-backend foundation. A deployment
administrator must create a separate writer login and grant it only
`primary_signal_cap_publication_write`. Do not use a migration or admin login
as the writer, and do not attach this capability to either web runtime. Before
deployment, authenticated service code must bind each publication decision to
the operator session and enforce its eligibility and audit rules. Automatic
publication also requires the product contract's evidence, safety, membership,
and hold gates.
