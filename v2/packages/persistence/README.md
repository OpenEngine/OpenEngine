## Persistence
`openengine-persistence`, the database layer every service shares. One setting
picks the backend for all of them:

| Variable | Default | |
| -- | -- | -- |
| `OE_DATABASE_BACKEND` | `sqlite` | `sqlite` or `postgres` |
| `OE_SQLITE_DIRECTORY` | `$XDG_STATE_HOME/openengine/databases` | one `<service>.sqlite3` per service |
| `OE_POSTGRES_URL` | | Postgres is a stub: selecting it fails on first use |

Each service's data is isolated. On SQLite it gets its own file (`cxn.sqlite3`,
`ge.sqlite3`, `vcp.sqlite3`); on Postgres the services share one database and
each gets a schema named after it, holding its tables and its Alembic version
table. Table definitions name no schema, so the same code runs on both.

A service keeps all of its database code in its `persistence/` directory:

```
persistence/
  __init__.py    PERSISTENCE = ServicePersistence("cxn", Path(__file__).parent, {
                     Backend.SQLITE: SqliteConnections, Backend.POSTGRES: PostgresConnections})
  schema.py      the service's tables, as SQLAlchemy metadata
  store.py       the records and the Protocol the rest of the service depends on
  sqlite/        the store, written for SQLite
  postgres/      the store, written for Postgres (stubs for now)
  migrations/    one Alembic history that runs on both
```

The rest of the service calls `PERSISTENCE.open()` and gets the configured
backend's store, typed as its Protocol. `oe migrate` brings every service's
database to head through `PERSISTENCE.upgrade()`; a new service joins by adding
its `PERSISTENCE` to the CLI's `migrations/databases.py`. Stores extend `DatabaseStore` and reach the database through
`self.database.transaction()` and `self.database.connect()`, which hand out
async SQLAlchemy connections. Timestamps use `UtcDateTime`, which comes back
timezone-aware on every backend.

### Changing a service's schema
Edit `schema.py`, then autogenerate a revision against an empty database and
review it:

```python
from pathlib import Path
from openengine.persistence import DatabaseConfig
from openengine.persistence.migrations import revision
from connection_bus.persistence import PERSISTENCE

database = PERSISTENCE.database(DatabaseConfig(sqlite_directory=Path("/tmp/oe-revision")))
revision(PERSISTENCE.migrations, "add connection secrets", database=database, autogenerate=True)
```

Revisions use SQLAlchemy and Alembic's portable operations, in batch mode so
column changes work on SQLite, and write shared column types as the plain types
they store so they never import service code. Each service's tests assert
`PERSISTENCE.drift(metadata) == []` after upgrading, so a schema change without
a matching revision fails.

### Schema documentation
`python -m openengine.persistence.documentation` writes, for every revision of
every service, `docs/schema/<service>/<revision>.md` (its SQL and the diff from
its parent) and `<revision>.mermaid`, an ERD highlighting what the revision
added, removed or changed. The head revision's ERD is also
`docs/schema/latest_<service>.mermaid`. The v2 schema documentation workflow
runs it whenever a migration changes, renders the changed ERDs to SVG with
Mermaid CLI, commits them and comments each service's new schema on the PR.
