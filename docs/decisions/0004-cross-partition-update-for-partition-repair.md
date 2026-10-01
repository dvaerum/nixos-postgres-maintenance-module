# 0004: Cross-partition `UPDATE`, not `DETACH`/`ATTACH`, for partition repair

## Decision

`partitions.repair_row()` relocates a misplaced row by issuing a
self-assignment `UPDATE` (`SET k = k`) through the **top-level (root)
partitioned table**, addressing the specific physical row via
`tableoid` + `ctid`, wrapped in `ALTER TABLE <child> DISABLE/ENABLE
TRIGGER USER`. It does **not** use `DETACH PARTITION`/`ATTACH
PARTITION`.

## What was tried first, and why it was abandoned

The original design (written into the project plan before
implementation) was: `ALTER TABLE parent DETACH PARTITION child`,
freely fix the row while the table carries no partition constraint at
all, then `ATTACH PARTITION ... FOR VALUES ...` to re-validate and
re-attach.

This was abandoned after real, empirical testing (not just reading
docs) showed it doesn't work as a *repair* mechanism at all:

- `ATTACH PARTITION` always re-scans and validates every row against
  the bound being attached. There is no way to make it skip a
  genuinely violating row.
- The commonly-cited "fast attach" trick -- add a matching `CHECK`
  constraint as `NOT VALID` before attaching, so Postgres trusts it and
  skips the scan -- does **not** apply here: confirmed by directly
  reproducing it that a `NOT VALID` constraint is not treated as
  "proven true" and does not suppress the scan. The real optimization
  that trick refers to requires an explicit `VALIDATE CONSTRAINT` step
  first, and *that* step scans and would reject a genuinely bad row
  just as directly. There is no bypass, by design -- see
  `docs/learnings/partition-repair-testing.md` for the full
  reproduction.

In short: Postgres's own partition-bound enforcement is airtight
against this approach. A misplaced row can only be moved by something
Postgres itself considers a legitimate write, not by any
detach/re-attach trick.

## What replaced it: Postgres's own cross-partition `UPDATE` routing

Since PG11, Postgres automatically relocates a row (`DELETE` + `INSERT`
under the hood) when an `UPDATE` to a partition key column causes it to
no longer satisfy its current partition's bound -- this is the
mechanism a normal application `UPDATE` relies on every day. Using it
here needed one more empirical correction: an `UPDATE` issued **directly
against the child relation** does not trigger this routing at all -- it
only re-checks that child's own bound and rejects the row outright.
Routing only happens when the `UPDATE` is issued through the
**top-level (root) partitioned table**, identifying the physical row
via `tableoid` + `ctid` (confirmed by direct reproduction; see
`docs/learnings/partition-repair-testing.md`). `repair_row()` therefore
always takes the root table as an explicit argument, separate from the
specific child/leaf being repaired (this also makes multi-level
sub-partitioning -- cycle 11 -- work without any special-casing:
routing from the absolute root descends through the whole tree
regardless of depth).

## `DISABLE`/`ENABLE TRIGGER USER`, not `ALL`

The self-assignment `UPDATE` still performs a real `DELETE` + `INSERT`
when a row moves, which would normally fire any user-defined trigger on
the source and destination partitions as a side effect of what is, from
the application's point of view, a no-op maintenance write.
`DISABLE`/`ENABLE TRIGGER USER` suppresses exactly those -- and
deliberately not `ALL`: `ALL` would also disable Postgres's own
internally-generated constraint triggers (foreign-key enforcement in
particular), which must stay active throughout. `BUG #18516` is the
real-world cautionary case: someone "optimized" a similar maintenance
operation to `DISABLE TRIGGER ALL`, silently lost FK enforcement for
the duration, and it was never auto-revalidated on re-enable. Cycle 8's
tests prove both halves of this empirically (a counter trigger proves
`USER` is actually suppressed; an `ON UPDATE CASCADE` foreign key
proves constraint enforcement is unaffected and doesn't needlessly
cascade when the value didn't really change).

## Detecting a misplaced row without parsing an error message

A naive design might try to catch `ATTACH`'s validation failure and
parse which row it names. Confirmed against PG16 source
(`tablecmds.c`) that this error ("partition constraint of relation ...
is violated by some row") never names the offending row -- only the
table. `partitions.misplaced_rows()` instead queries
`pg_get_partition_constraintdef()` directly against each leaf and finds
every row that currently fails it (`WHERE NOT (<constraint>)`) -- a
direct, row-level answer that doesn't depend on provoking and parsing
an error at all.
