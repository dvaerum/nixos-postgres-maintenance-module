# Opting out of partition repair entirely -- check/refresh only the
# cluster's own Postgres-tracked collation versions, skip the separate
# text-partition bound check. See
# docs/decisions/0004-cross-partition-update-for-partition-repair.md
# for what this disables.
{ ... }:
{
  services.postgresqlCollationGuard = {
    enable = true;
    partitionRepair.enable = false;
  };
}
