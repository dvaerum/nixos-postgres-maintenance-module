# `REFRESH COLLATION VERSION` rejects NULL-vs-non-NULL transitions

`ALTER DATABASE ... REFRESH COLLATION VERSION` (and the equivalent for
named collations) refuses to update the recorded version if the
recorded and actual versions disagree on whether they're `NULL` at
all. Confirmed in PG16 source
(`dbcommands.c:AlterDatabaseRefreshColl()`):

```c
/* cannot change from NULL to non-NULL or vice versa */
if ((!oldversion && newversion) || (oldversion && !newversion))
    elog(ERROR, "invalid collation version change");
```

The error text is exactly `invalid collation version change`, with no
further detail.

## When this actually happens

A database's *actual* version is `NULL` whenever its collation is
`C`/`C.*`/`POSIX` (Postgres never versions these at all), and non-`NULL`
for any other real libc or ICU locale. So this error fires whenever the
*recorded* `datcollversion` doesn't match that same NULL-ness --
concretely:

- A `C`-locale database/collation whose recorded version was somehow
  set to a non-`NULL` value (shouldn't happen organically, but
  `collation_guard`'s own test suite constructs exactly this state
  deliberately, see below).
- The reverse: a real-locale database/collation whose recorded version
  was cleared to `NULL`.

A `REFRESH` between two *real*, non-`NULL` versions (e.g. a genuine
glibc upgrade) is always fine -- this only bites the NULL-ness
transition specifically.

## Why this matters for this project

`collation_guard.collation._safe_refresh()` exists specifically to
catch this (and report it via `DatabaseResult.refresh_error` /
`process_template0()`'s return value) rather than let a real production
failure mode crash the guard.

It also has a sharp, non-obvious consequence for test fixtures: **once
a cluster's `template0` carries a real (non-`NULL`) version, Postgres
refuses to create *any* database with a differently-versioned locale
from it** -- `CREATE DATABASE ... TEMPLATE template0` itself checks
`template0`'s own recorded-vs-actual NULL-ness consistency before
copying it, and errors with `template database "template0" has a
collation version, but no actual collation version could be determined`
if it's inconsistent. This is why `tests/conftest.py` maintains **two
separate** ephemeral clusters (`pg_dsn`, initialized with a real libc
locale, and `c_locale_pg_dsn`, initialized with `C`) rather than one:
a single cluster can't exercise both a genuine libc-locale refresh
*and* a from-scratch `C.UTF-8` database creation once its `template0`
has been touched.
