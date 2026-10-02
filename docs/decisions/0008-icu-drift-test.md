# 0008: A real, permanent ICU-drift test for partition repair

## Decision

`flake.nix` builds two full Postgres binaries, each linked against a
different ICU release (`postgresqlIcu72`/`postgresqlIcu73`, both
`pkgs.postgresql_17.override { icu = ...; }`).
`tests/nixos/icu-drift.nix` initializes and populates a partitioned
table under one binary, then starts the *exact same* on-disk `$PGDATA`
under the *other* binary, and runs the real `collation-guard` CLI
against the result. This closes README's former "No real-drift test
coverage for row-level partition repair" limitation, for ICU only --
see `docs/learnings/partition-repair-testing.md` for the full history
of why this was previously believed unreproducible, and the glibc
reasoning below for why that half of the gap stays open.

Exposed as `packages.icuDriftTest`, not as a `checks` entry -- run by
hand via `nix build .#icuDriftTest -L`, not part of `nix flake check`
or CI. See "Why not wired into CI" below.

## Why ICU, not glibc

Both are real collation providers this project supports, but only ICU
is practical to multi-version here:

- **ICU versions are ordinary, independent nixpkgs packages**
  (`icu72`, `icu73`, ...) that a single Postgres derivation can be
  pointed at via `postgresql_17.override { icu = ...; }`. Building two
  Postgres binaries against two different ICU releases is a normal,
  cheap nixpkgs override -- no toolchain or system changes.
- **glibc is the C standard library the entire nixpkgs closure (every
  tool in the test, including Postgres itself, `bash`, `coreutils`)
  links against.** Multi-versioning it would mean either building a
  second, almost entirely separate nixpkgs closure pinned to an older
  glibc, or bind-mounting/`patchelf`-ing a whole separate libc
  environment -- qualitatively heavier and far more fragile than
  swapping one shared library override.

The README's "Known limitations" entry already draws this same line
("a genuinely misplaced row ... not reproducible in a single-locale
test sandbox"); this ADR closes it for the half that's actually
tractable and leaves the other explicitly open, rather than silently
downgrading the glibc claim without having done the glibc-equivalent
of the work below.

## The multi-version build and binary-swap mechanism

Empirically validated step by step (scratch flake, three ICU releases,
real Postgres builds) before being encoded permanently:

1. **Patching the shared library in place does not work.** Swapping
   `icu73`'s `.so` underneath an `icu72`-linked `postgres` binary via
   `patchelf --replace-needed` fails outright:
   `undefined symbol: uloc_countAvailable_72` -- ICU bakes its release
   number into every exported symbol name (its own "renaming" scheme),
   so a binary linked against one release can never dynamically load
   another release's library at all. There is no way to keep one
   binary and swap only the library.
2. **Swapping which whole binary manages the same `$PGDATA` does
   work.** Two separately-built Postgres binaries (same major version,
   17.11, different `icu` override) are both perfectly willing to
   start against the identical on-disk cluster -- Postgres's on-disk
   format doesn't encode the ICU library's own version, only each
   collation's *recorded* `collversion`, which is a plain catalog
   value. Confirmed starting an icu72-initialized `$PGDATA` under the
   icu73-linked binary: it starts clean and immediately logs a real,
   Postgres-native collation-version-mismatch warning for an affected
   locale.
3. **The specific string pair and locale close the hardest case, not
   an easy one.** ICU-22544 documents that ICU 72->73 reordered
   root/language-collation comparisons for hundreds of locales
   *without* bumping `collversion` for many of them -- confirmed
   directly against a `de`-locale ICU collation here too:
   `collversion` reads `153.120` under both builds, byte-identical,
   while `U+2018` (') and `U+201A` (‚) swap relative sort order between
   them. This is exactly this project's own documented blind spot
   (README "Known limitations", the ICU-22544 bullet): Postgres's own
   collversion-tracked mismatch detection cannot see this drift at
   all. The only reason `partitions.py`'s `misplaced_rows()` still
   catches the resulting misplaced row is that it re-checks the row
   against a *live* comparison (`pg_get_partition_constraintdef`),
   never the stored version number -- so this test proves the
   strongest, not weakest, form of the claim.
4. The partition bound and inserted value use Postgres's own `U&'...'`
   Unicode string-escape syntax
   (https://www.postgresql.org/docs/17/sql-syntax-lexical.html, "String
   Constants with Unicode Escapes") (`U&'A\2018B'`, `U&'A\201A2'`) so
   the test file itself stays plain ASCII, with no exotic Unicode
   punctuation embedded in Nix/shell-quoted strings.

## Why not wired into CI

`icu72`/`icu73` themselves are ordinary cached nixpkgs packages (both
substituted from `cache.nixos.org` at the pinned `flake.lock` revision,
confirmed -- no source build needed for either), but **the Postgres
override itself is not cached**: `postgresql_17.override { icu = ...;
}` is a one-off derivation nobody else's CI builds, so `nix build`
compiles Postgres from source, twice (once per ICU release), on every
single invocation -- a real, repeated cost with no cross-run cache on
ephemeral GitHub Actions runners.

`ci.yml` and `ci-stable.yml` both run a bare `nix flake check -L` on
every push and PR -- any `checks` entry is unconditionally included,
and there is no selective `--exclude-check`-style flag for `nix flake
check`. `ci-stable.yml` additionally overrides the `nixpkgs` input to
whatever the current stable branch resolves to that day, so it would
never get a cache hit across runs even if the regular `ci.yml` build
were otherwise cacheable. Doubling two from-source Postgres
compilations across both workflows, on every push, to guard a code
path that only moves when `partitions.py`/`collation.py` or the ICU
override itself changes, is not a good trade -- this is exactly the
kind of heavy, infrequently-needed tier `tests/nixos/*.nix` already
exists to separate out (see 0005), just one notch further: expensive
enough that even the slow tier's own `checks` wiring isn't the right
home for it.

If `icu72`/`icu73` builds of `postgresql_17` are ever added to a wider
binary cache this project's CI can rely on, or GitHub Actions gains a
cheap, persistent Nix store cache this project adopts for other
reasons, revisit wiring `icuDriftTest` into `checks` (or a separate,
manually-triggered `workflow_dispatch` job) rather than leaving this
reasoning stale.

## Alternatives considered

- **Hand-call `partitions.py` functions directly** instead of running
  the real `collation-guard` binary end-to-end. Rejected: the real CLI
  is what's shipped, and running it directly also exercises
  `main.py`'s env-var plumbing and `collation.py`'s reindex path
  against the same drifted cluster for free -- which is in fact how
  this test found and closed a second, independent bug (see below),
  something a hand-called `repair_partition_table()` alone would never
  have surfaced.
- **A single ICU version with a hand-corrupted `collversion`.** This is
  exactly what `tests/test_collation.py`'s existing `_fake_stale()`
  helper already does for the Postgres-tracked (collversion-based)
  detection path -- it proves that logic, but can never produce a row
  that's genuinely misplaced by a real comparison change, which is the
  entire gap this ADR closes.

## A bug this test found along the way

Building this surfaced an independent, genuine bug, unrelated to ICU:
`collation.py`'s `user_tables()` (via `pg_tables`) handed a
partitioned table's own parent entry (`relkind = 'p'`) to `REINDEX
TABLE` directly, which Postgres rejects outright ("REINDEX TABLE
cannot run inside a transaction block") since reindexing a partitioned
table parent needs multiple internal transactions. This would have
fired on *any* stale-collation reindex that happened to touch a
partitioned table, independent of ICU -- fixed by querying `pg_class`
with `relkind = 'r'` directly instead of `pg_tables` (which has no
relkind column to filter 'p' back out). Shipped as its own commit
ahead of this one, with its own red/green test in
`tests/test_collation.py`.
