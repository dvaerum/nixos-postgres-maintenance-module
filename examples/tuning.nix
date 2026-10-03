# Tuning the two bounded-concurrency/retry knobs together. Both are
# caps on I/O-bound work that shares one Postgres instance's disk I/O,
# shared buffers, and WAL writer -- see docs/decisions/0006 for why
# each is bounded rather than unbounded.
{ ... }:
{
  services.postgresqlCollationGuard = {
    enable = true;

    # Raise only if the cluster has many databases and spare I/O
    # headroom -- the work is I/O-bound, so unbounded parallelism can
    # make the whole run slower, not faster.
    maxParallelDatabases = 8;

    partitionRepair = {
      enable = true;
      # The default (3) is a proven bound (two passes always suffice
      # given connectionLockdown excludes every other writer -- see
      # docs/learnings/partition-repair-convergence.md) plus one pass
      # of margin, not a target to raise for a bigger table -- the
      # pass count doesn't scale with how many rows are misplaced.
      # Raise it only alongside connectionLockdown.enable = false,
      # where a concurrent writer really can introduce new drift
      # mid-repair and the two-pass proof no longer applies.
      maxRepairAttempts = 5000;
    };
  };
}
