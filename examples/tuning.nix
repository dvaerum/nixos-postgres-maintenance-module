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
      # The default (1000) is a safety cap, not a target -- raise it
      # only once a specific partitioned table is known to have more
      # misplaced rows than that after a real collation change.
      maxRepairAttempts = 5000;
    };
  };
}
