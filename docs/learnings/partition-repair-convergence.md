# Why the partition-repair loop converges in at most two passes

`repair_partition_table()` loops `_repair_one_pass()` (one full scan of
every leaf partition) until a pass moves nothing, capped at
`maxRepairAttempts`. It's tempting to assume a misplaced row might need
several passes to "settle" -- it doesn't, given two invariants this
project already guarantees elsewhere:

1. **A row always moves to its true final home in one hop.**
   `repair_row()`'s cross-partition `UPDATE` is always issued against
   the *root* table, and Postgres's routing descends through the whole
   partition tree from there (confirmed in
   `docs/decisions/0004-cross-partition-update-for-partition-repair.md`).
   There is no intermediate, still-wrong landing spot.
2. **Nothing else is writing during repair.** `connectionLockdown`
   (default on) rejects and terminates every other connection to the
   database for the duration of the repair, so no new drift can appear
   mid-run (`docs/decisions/0007-connection-lockdown-during-repair.md`).

Given both, trace what a pass actually does: it visits each leaf once,
in some fixed order, and fixes whatever's wrong in that leaf *at the
moment its turn comes*. The only reason a pass's "rows moved" count can
be non-zero more than once is leaf-visit order, not a row genuinely
needing to move twice:

```
leaves visited in order:  L1 → L2 → L3 → L4 → L5
                                     │
pass 1:  ... L4's turn: finds a row that belongs in L2, moves it
         there (one hop, correctly placed -- for good) ...
                                     │
         L2's turn already happened (position 2 < 4) -- it won't be
         re-scanned again THIS pass, even though it just received a
         new, already-correct row.
                                     │
         → this pass's moved-count is non-zero → loop asks for one
           more pass, purely to confirm
                                     │
pass 2:  scans L1, L2 (sees the row that arrived last pass -- it's
         already correct, satisfies L2's own constraint, not
         misplaced) ... L5 → moved = 0 → done
```

This holds regardless of how many rows are scattered across however
many leaves, or what order they're visited in: every row that will
ever need to move already needs to at the start (invariant 2), and
each one resolves in exactly one `repair_row()` call during pass 1
(invariant 1). Pass 2 is never doing new work, only confirming pass 1
left nothing behind.

`services.postgresqlCollationGuard.partitionRepair.maxRepairAttempts`
defaults to 3 -- this proven bound plus one pass of margin. Hitting the
cap is a signal that one of the two invariants above didn't hold (e.g.
`connectionLockdown` disabled, or a bug) -- not evidence that the
table just needed more time -- so it fails loudly rather than retrying
up to some much larger number.
