# Latest database schema

[schema.mermaid](schema.mermaid) is an ERD of the latest Alembic schema on this
branch, replayed into empty SQLite databases. It includes every application table
and column from the state store (`state_`) and graph runtime store (`graph_`).
The prefixes distinguish tables in separate databases; they are not part of the
SQL table names. The revision heads are recorded in the chart comments.

PK, FK and UK mark declared primary, foreign and unique keys. Multiple PK columns
form one composite primary key. Multiple UK columns can belong to separate unique
constraints; consult the [revision SQL](../schema/) for exact constraint groups.
Column comments show nullability. Solid edges show declared foreign keys; dashed
edges show selected logical references used by the application, which SQLite does
not enforce as foreign keys. Logical references are not marked FK on columns.

The reserved PostgreSQL history has no application tables. LangGraph's separately
managed checkpoint tables are outside these Alembic histories. This is a current
schema overview; revision-by-revision SQL remains available in `docs/schema`.
