# Testing partition-bounds drift repair

A genuinely misplaced partition row only exists in production because
the *same* collation's comparison behavior changed between the row's
last validation and now (a real glibc/ICU library version change).
That can't be reproduced in a single-locale test sandbox, and -- this
took real effort to confirm -- there is no SQL/tool-level shortcut
either. The `ATTACH PARTITION` dead end -- its unavoidable
re-validation scan, and why a `NOT VALID` `CHECK` constraint doesn't
suppress it -- is covered in full in
`docs/decisions/0004-cross-partition-update-for-partition-repair.md`;
the other avenue ruled out, not covered there:

- `pg_surgery` -- the real contrib extension built for forcing
  corruption-repair writes into a heap -- only ships
  `heap_force_freeze`/`heap_force_kill` in this nixpkgs build, not a
  force-insert equivalent. It can't be used to seed a bad row either.

Confirmed empirically along the way (not from docs, from directly
reproducing it against a live cluster): an `UPDATE` issued **directly
against the child relation** does not get Postgres's cross-partition
row-movement behavior (PG11+) at all -- it only re-checks that child's
own bound and rejects the row outright if it fails, never considering
sibling partitions. Row movement only happens when the `UPDATE` is
issued through the **top-level (root) partitioned table**, identifying
the physical row via `tableoid` + `ctid`:

```sql
UPDATE root_table
SET k = k
WHERE tableoid = 'child_name'::regclass AND ctid = '(0,1)'::tid;
```

This is why `collation_guard.partitions.repair_row()` takes a
`root_table` argument distinct from the `child` it's repairing, and why
its test doesn't try to fake a drifted row at all -- it proves the two
load-bearing halves separately instead:

1. `misplaced_rows()` correctly reports "nothing wrong" against a
   known-clean partition (the part that's fully testable).
2. The row-movement mechanism `repair_row()` depends on really works
   when accessed through the root table, proven with an explicit value
   change instead of a no-op self-assignment (since a true
   same-value-different-comparison case can't be constructed here).

A real end-to-end reproduction of a stale-collation misplaced row would
need two genuinely different glibc/ICU builds compared against the same
on-disk data. **Closed for ICU** by `tests/nixos/icu-drift.nix` and
`docs/decisions/0008-icu-drift-test.md`: two full Postgres builds, each
linked against a different ICU release, swapped against the same
on-disk `$PGDATA` -- proves a genuinely misplaced row (confirmed via a
live collation-comparison change, not just a `collversion` bump) is
correctly detected and repaired by the real `collation-guard` CLI.
**Still open for glibc** -- see 0008's "Why ICU, not glibc" for why
that half stays out of scope: glibc is the C library the entire
nixpkgs closure links against, not a single swappable override the way
`postgresql_17.override { icu = ...; }` is.
