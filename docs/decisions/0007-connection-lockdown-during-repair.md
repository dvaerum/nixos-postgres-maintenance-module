# 0007: Connection lockdown during repair

## Decision

Reject new connections to a database for exactly the duration it's
actively being reindexed/repaired, and terminate any session already
connected to it at that moment -- scoped to only the databases that
actually need work, for only as long as each one's own work takes. A
single dedicated thread (`LockdownManager`) owns a `pg_hba.conf`
include file and all lock/unlock SQL; the existing `ThreadPoolExecutor`
workers send it `lock`/`unlock` requests over a queue instead of
touching either directly. Default `true`
(`services.postgresqlCollationGuard.connectionLockdown.enable`).

## Why

README's own "Future work" section flagged a real, unsolved gap, but
with the wrong scope: "a client on another host." Confirmed directly
against nixpkgs's `services.postgresql` module: it's `Type=notify`, and
the real `postgres` binary sends `READY=1` only once it's already
accepting client connections. `postgresql-collation-guard.service` only
has `After=postgresql.service`, so by the time it starts, Postgres is
already listening. The actual gap is **anything not itself ordered
`After=postgresql-setup.service`/`postgresql.target`** -- a cron job, a
manually-run `psql`, a service with only `Wants=postgresql` -- local or
remote, it doesn't matter. Nothing stopped such a client from
connecting mid-REINDEX or mid-partition-repair, against inconsistent
state.

## `pg_hba.conf` + a single-writer thread, not `ALTER DATABASE ... CONNECTION LIMIT`

`CONNECTION LIMIT` was seriously considered and empirically tested
(against a real ephemeral `initdb`/`pg_ctl` cluster) before being
rejected:

- It *can* be set from a connection already inside the target database
  (verified: `ALTER DATABASE lockme CONNECTION LIMIT 0` run from a
  session connected to `lockme` succeeds and doesn't kill that same
  session), and a non-superuser connection attempt is correctly
  rejected afterward with no reload needed.
- But **superuser connections are entirely exempt from `CONNECTION
  LIMIT`** (verified empirically, matches documented Postgres
  behavior) -- a real gap relative to `pg_hba.conf`'s `reject` method,
  which rejects anyone, superuser or not.
- More importantly: achieving true per-database lock *duration* (a
  database unlocks the instant its own work finishes, not when the
  whole batch finishes) means each worker thread would mutate its own
  database's `CONNECTION LIMIT` independently -- no shared-file race
  there, since each database's limit is its own row in `pg_database`,
  but it loses the "one thing to clean up on crash" property entirely:
  a crash-recovery step would need to know exactly *which* databases
  were mid-lock at the moment of the crash and restore each one's limit
  individually.

`pg_hba.conf` tightening keeps the "one file, one crash-cleanup action"
property regardless of how many databases are locked at any moment.
`hba_file` itself is fixed at server start and NixOS points it at a
read-only Nix store path (verified directly against
`nixos/modules/services/databases/postgresql.nix`) -- but
`include_if_exists <path>` directives are re-resolved on every reload,
not just at startup, and `services.postgresql.authentication` is
additive (`types.lines`, merged via `lib.mkBefore`), so this project's
own module adds one `include_if_exists` line, ahead of every other
rule, with zero required config from the consumer. The file doesn't
exist on a normal day, so this is a silent no-op until something is
actually locked.

## Single-writer thread, not per-worker file access

The existing `ThreadPoolExecutor` processes up to `maxParallelDatabases`
databases concurrently. If each worker rewrote the shared lockdown file
directly, that's a real write race with no natural serialization.
`LockdownManager` runs in one dedicated thread, processing a FIFO queue
of `lock`/`unlock` requests one at a time -- no locks/mutexes needed
anywhere else, since there is exactly one writer by construction.
`lock()`/`unlock()` block the calling thread until genuinely applied,
so a worker never starts mutating a database before it's actually
locked.

## `pg_conf_load_time()`, not `pg_hba_file_rules`, confirms a reload completed

`pg_reload_conf()` only *requests* a reload (sends SIGHUP) -- it's
asynchronous, with no built-in way to synchronously confirm the
postmaster has actually finished re-reading `pg_hba.conf`. A first
attempt polled `pg_hba_file_rules` after reload, on the theory that it
reflects the live, loaded ruleset. That was a dead end: the view
reflects a fresh parse of the file **on disk**, not the postmaster's
active ruleset, so it resolved instantly regardless of whether a
reload had actually happened -- confirmed by reproducing the exact
race it was meant to close (8 threads, rapid lock/unlock cycles; a
brand-new connection occasionally still succeeded against stale rules
for a short window after `lock()`/`unlock()` returned).

`pg_conf_load_time()` is the authoritative signal: Postgres updates it
only once a reload has genuinely completed. `LockdownManager` captures
it before calling `pg_reload_conf()`, then polls (short interval, 2s
timeout) until it changes, before treating the request as applied. The
single-writer-thread design means there's never a second reload in
flight while polling for the first one's completion, so there's no
ambiguity about why it changed. Verified: 23 consecutive clean runs of
the full concurrency test after this fix, versus consistent failure
before it.

## `application_name` exclusion -- and the self-inflicted-harm bug it has to avoid

Every connection the guard opens is tagged
`application_name=collation-guard`. The termination sweep excludes by
this tag, not by a single PID:

```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE datname = %s AND application_name <> 'collation-guard'
```

This matters because the guard can hold more than one connection to
the *same* database at once -- `postgres` itself, when it happens to be
a `C.UTF-8` database, gets reindexed in the glibc-stamp phase while
`admin_conn` (held open for that whole phase) is also connected to it.
A naive `pid <> pg_backend_pid()` exclusion only protects the one
connection issuing the terminate query, not any *other* guard
connection to the same database -- it would have killed `admin_conn`.

`application_name` only helps for sessions that are **already open**
when `lock()` runs, though -- `pg_hba.conf` evaluates `database`/`user`/
`address` at authentication time, before `application_name` is even
known to the server, so it can't exempt a **brand-new** connection
attempt the way it exempts an existing session from the termination
sweep. This surfaced as a real, caught bug: the glibc-stamp phase's
first implementation called `manager.lock(dbname)` *before* opening the
per-database connection for that loop iteration -- when `dbname ==
"postgres"`, that new connection attempt hit its own just-written
reject rule. Fixed to match `_process_database`'s already-correct
order: connect first, lock second (from inside the now-open
connection), work, unlock. Reproduced directly via a third ephemeral
test cluster whose default locale is `C.UTF-8` from `initdb`, so
`postgres` is a genuine member of `c_utf8_databases()`.

## Lock only what actually needs it

Per-database locking (`_process_database`) is driven by the same cheap,
already-existing detection functions
(`database_collation_is_stale`/`stale_named_collations`/
`partition_repair_candidates`) used to decide whether a reindex/repair
is needed at all -- a database with nothing to fix never calls
`lock()`, proven via a real, pre-existing connection surviving
untouched throughout the run. The glibc-stamp phase has no equivalent
per-database filter to apply: Postgres never versions `C`/`C.*`/`POSIX`
at all, so once the cluster-wide stamp itself is stale, every member of
`c_utf8_databases()` genuinely gets reindexed and is locked for its own
turn.

## Default `true`, despite being new behavior for every existing deployment

Closing a real correctness gap, not an opt-in extra -- but still a
real, explicit option (`connectionLockdown.enable`), since a deployment
with a specific reason to allow concurrent connections during the
guard's run should be able to say so rather than being surprised by
this on the next upgrade.
