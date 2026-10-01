# 0006: Hooks redesign (one shape, one code path) and parallel per-database processing

## Decision

Replace the original four hooks
(`preStart`/`onSuccess`/`onFailure`/`postRun`, each a bare
`listOf package` invoked via systemd `ExecStartPre`/`ExecStartPost`/
`ExecStopPost`/`OnFailure=`) with a single, richer `hookType`
(`path`, `args`, `environment`, `environmentFile`, `blockOnFailure`),
used identically by all seven hook lists -- the original four plus a
new `perDatabase.{preStart,onSuccess,onFailure}` tier. Every hook,
**including the systemd-triggered `onFailure` one**, is invoked through
the same `collation_guard.hooks.run_hook()` function. Process
databases concurrently (`ThreadPoolExecutor`, bounded) instead of one
at a time.

## Why

The motivating use case is notifications that know which database (if
any) they're about and can carry a real error message, with secrets
passed safely rather than baked into the Nix store. That needs
arguments, an `environmentFile`, and inline `environment` variables --
none of which a bare package gives you -- and it needs a per-database
hook tier, since the original four only ever fired at the whole-run
level.

## Env-var collision = hard error, not "last one wins"

Every hook invocation merges three sources: the stage's own default
variables (`COLLATION_GUARD_STAGE`/`DATABASE`/`CONTEXT`/`ERROR`),
`environmentFile`, and inline `environment`. If the same key appears in
more than one of these, `merge_environment()` raises
`EnvironmentCollisionError` -- the hook never runs. Silently letting
one source shadow another is exactly the kind of bug that stays
invisible until the wrong value reaches a hook in production; this
makes it a loud, immediate failure instead. The *ambient* process
environment (`PATH`, `HOME`, `PGHOST`/`PGPORT`/etc.) is deliberately
excluded from this check -- a hook overriding `PATH` is normal and
expected, and including it would make the collision rule impossible to
satisfy for almost any hook.

## `blockOnFailure` has no default, and is never a no-op

The `hookType` field `blockOnFailure` is `nullOr bool`, default `null`
-- but `null` is deliberately invalid at use: every configured hook is
validated (`nixosModule/validate-hook.nix`) and evaluation `throw`s,
naming the option path, if it's left unset. This forces an explicit
choice at configuration time rather than a silent default a future
reader would have to go dig up.

Every one of the seven hook points gives `blockOnFailure = true` a
concrete, different, but always real consequence -- never "nothing
happens":

| Hook point | `blockOnFailure = true` consequence |
|---|---|
| `preStart` (global) | Aborts the whole run immediately, before any database is touched. |
| `onSuccess` (global) | Adds a failure to an otherwise-clean run, flipping its exit code to 1. Only fires when every database succeeded -- unlike `postRun`. |
| `postRun` (global) | Adds a failure *after* every database already finished -- can flip an otherwise-clean run's exit code even though the real work was fine. |
| `onFailure` (global, systemd-triggered) | Makes the companion unit itself report failed status (`systemctl --failed`) -- there's nothing left to block booting at this point, so this is about making a broken notification path visible, not gating anything. |
| `perDatabase.preStart` | Skips processing that one database entirely; every other database in the same run is unaffected. |
| `perDatabase.onSuccess` | Adds a failure for a database whose actual Postgres processing was clean -- for a notification that's itself load-bearing. |
| `perDatabase.onFailure` | Adds a *second*, distinct failure entry alongside the database's original one -- both visible independently. |

## `onFailure`'s trigger stays systemd-native; its execution doesn't

`OnFailure=` is the one guarantee a dead process can't arrange for
itself -- it fires even if the guard is killed or crashes outright, not
just on a clean non-zero exit. That has to stay systemd-level. But
there's no reason the *hooks themselves* need a different shape or a
separate implementation: the companion unit's entire `ExecStart` is
`collation-guard --on-failure`, a second, equally small entry point
into the same binary (`main.run_on_failure()`) that reads the same
`COLLATION_GUARD_HOOKS_FILE` and the last-written
`COLLATION_GUARD_CONTEXT_FILE` to recover what failed, then calls the
exact same `run_hook()` as every other stage. A missing or unreadable
context file (a crash on the very first-ever run, before anything was
ever written) falls back to a generic error message rather than
raising -- this entry point must never itself crash.

An earlier draft of this design kept `onFailure` genuinely separate --
a different hook shape, a companion unit built per-hook, a
`--print-failure-env` helper to synthesize an `EnvironmentFile=` for
systemd's own (weaker, non-strict) `Environment=`/`EnvironmentFile=`
merge semantics. That was unnecessary complexity solving a problem that
doesn't exist: crash-safety only requires the *trigger* to be
systemd-native, not the hook execution underneath it. Caught and
corrected during the plan review for this feature, before any of it
was implemented.

## Threads, not multiprocessing or asyncio, for parallel databases

The work is I/O-bound (waiting on Postgres over a socket for
REINDEX/queries), so `ThreadPoolExecutor` is sufficient -- Python's GIL
doesn't matter when threads are mostly blocked on network I/O, and it
avoids the serialization/IPC overhead multiprocessing would add for no
benefit here. Bounded (`maxParallelDatabases`, default 4), not
unbounded: REINDEX is I/O- and CPU-heavy on the Postgres side, and
every database shares the same instance's disk, CPU, shared buffers,
and WAL writer -- unbounded parallelism on a cluster with many
databases could make the whole run slower, not faster, by contending
for those shared resources. Same reasoning `vacuumdb -j`/`pg_restore
-j` cap their job count rather than defaulting to "all at once."

`_process_database()` returns its own `RunReport` rather than mutating
a shared one, so each worker thread only ever touches memory it owns.
`run()` merges every worker's result into the real report sequentially,
back in the main thread, once every worker has finished -- no locking
needed anywhere. Futures are submitted in sorted database-name order
and merged by iterating that same list (not `as_completed()`), which
blocks per-future without stalling the others from continuing to run
concurrently -- so the final report is fully deterministic regardless
of which thread actually finishes first.

## `COMMENT ON DATABASE postgres`: preserve/append, not clobber

Found while reviewing this feature, not originally in scope: the
existing C.UTF-8 stamp mechanism (`collation.glibc_stamp()`/
`set_glibc_stamp()`) treated the whole `postgres` database comment as
its own -- reading it only recognized a stamp if the *entire* comment
started with the prefix, and writing it unconditionally replaced the
whole comment. `COMMENT ON` has no native append/merge; it always sets
the full text. If anything else (a DBA note, another tool, or -- as
confirmed live in this project's own container tests -- NixOS's own
`services.postgresql` module, which sets a default comment on this
exact database) ever put a comment there, this guard would have
silently destroyed it with no trace.

Fixed to treat the stamp as one line *within* the comment,
found/replaced by pattern rather than assumed to own the whole string.
A pre-existing comment gets the stamp line appended after it on the
first write, and updated in place -- not duplicated -- on every write
after that.
