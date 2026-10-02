# 0002: Reindex per-relation, not `REINDEX DATABASE`

## Decision

`collation.reindex_all_user_tables()` loops over every user table and
reindexes it independently (`REINDEX TABLE`), catching and isolating a
failure per table, rather than issuing one `REINDEX DATABASE` call for
the whole database.

## Why

`REINDEX DATABASE [CONCURRENTLY]` aborts **entirely** on the first bad
index. A single duplicate-key violation on one table (the kind a
genuine collation drift can introduce) stops the whole command --
every other, perfectly fixable index in that same database never gets
touched, even though nothing is wrong with them.

This is not a theoretical concern: GitLab hit it in production and
documented it in their own incident-response runbook
(`gitlab-org/gitlab#370622`, `gitlab-org/gitlab#505982`). Their
recovery procedure is a manual table-by-table loop for exactly this
reason -- there is no flag or mode of `REINDEX DATABASE` that changes
this behavior. The official reference
(https://www.postgresql.org/docs/17/sql-reindex.html) documents what
`REINDEX DATABASE` does, not this all-or-nothing failure behavior --
the GitLab incident above is the evidence for that part, not the docs.

## Consequence: a partial failure must not refresh anything

Because reindexing is now per-table, a database can have some tables
succeed and others fail. `process_database()` only refreshes the
recorded collation version (`ALTER DATABASE ... REFRESH COLLATION
VERSION`, `ALTER COLLATION ... REFRESH VERSION`) once **every** table
reindexed cleanly -- a partial failure means the database's content
hasn't been fully verified under the current collation, so its
recorded version must stay stale, not be marked current on a
technicality. Cycle 4's regression test
(`test_process_database_reindex_failure_is_isolated_per_relation`)
proves this directly: a fixture with one clean table and one
deliberately-broken table (via the `pg_index.indisready` toggle
technique) asserts the clean table still gets reindexed and the
database's version stays stale overall.

## Known related gap, not yet fixed

The in-tree guard this project replaces
(`nixos-developer-system`'s `common/host/postgresql/collation-guard.sh`)
has the same `REINDEX DATABASE "$db"` call and the same gap. Low
current blast radius there (every live database on that host has only
a handful of objects today), but it's a real gap in already-deployed
production code -- worth a small, independent patch whenever
convenient, not gated on this project.
