# 0003: Compare the glibc *store path*, not a version string

## Decision

`collation.glibc_stamp()`/`set_glibc_stamp()` record and compare
`config.i18n.glibcLocales`'s actual Nix store path (e.g.
`/nix/store/qfxqcs...-glibc-locales-2.42-84`), not a bare version
string like `"2.42"`.

## Why

Postgres never records a version for `C`/`C.*`/`POSIX` locales at all
(`get_collation_actual_version()` returns `NULL` for them regardless of
library version -- confirmed against PG16 source, not stated in the
official docs for `datcollversion`/`collversion`:
https://www.postgresql.org/docs/17/catalog-pg-database.html,
https://www.postgresql.org/docs/17/catalog-pg-collation.html). A
database using `C.UTF-8` can drift with zero signal from Postgres's
own catalogs, so this project has to track the underlying glibc build
itself.

A bare version number is not a safe comparison key for that purpose:
nixpkgs#245360 (fixed in commit `43da9e8ff`, "glibcLocales: disable
parallelism to restore deterministic locales") documented the *same*
glibc version producing a *different*, non-deterministically-built
locale archive for roughly six weeks in 2023. Two builds that both call
themselves `"2.42"` are not guaranteed to be byte-identical. The store
path changes on any content difference, version bump or not -- it's
the only identifier that's actually trustworthy here.

## Storage: `COMMENT ON DATABASE postgres`, not a `$PGDATA` file

The stamp is stored via `shobj_description()`/`COMMENT ON DATABASE
postgres` -- a shared `pg_shdescription` catalog entry, cluster-wide,
survives `pg_dumpall`/`pg_upgrade`. A flat file under `$PGDATA` was
considered and rejected: it's one accidental `rm`, or one backup that
forgot to include it, away from losing the only evidence a reindex is
still owed.

## Missing or stale: reindex unconditionally, no fresh-cluster exception

If the stamp is missing (a brand-new cluster, or one that predates this
guard) or doesn't match the current store path, every `C.*`-locale
database gets reindexed -- including on a fresh cluster. A brand-new,
mostly-empty database costs nothing to reindex; "we don't know whether
this already silently drifted before the guard existed" is exactly the
unsafe state this mechanism exists to remove, not a case to
special-case away.

## A parallel finding, not solved by this mechanism

ICU collations have an analogous, *silent* version-tracking gap:
`postgresql.verite.pro` documented ICU 72→73 changing root-collation
sort order for 331+ locales via a CLDR data cherry-pick into a point
release, without bumping `collversion` at all -- ICU's own bug tracker
(ICU-22544) calls this "the first time in ten years" that happened.
Unlike the `C.UTF-8` case, there is no equivalent stamp this project
can compare for ICU, because ICU's version string *did not change*
when the behavior did. See `docs/learnings/` for this documented as an
accepted gap, not attempted here (this fleet's own databases don't use
the `icu` provider).
