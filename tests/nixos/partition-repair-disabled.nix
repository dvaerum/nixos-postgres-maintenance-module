# partitionRepair.enable = false: its own container, not merged into
# examples.nix's shared one, because tuning.nix already sets
# `partitionRepair.enable = true` (redundantly, matching the default)
# in that same shared config -- two modules setting the same leaf
# option to different literal values is a hard eval conflict, not
# something the module system merges.
{ nixosModule }:
let
  examples = import ../../examples;
in
{
  name = "collation-guard-partition-repair-disabled";

  containers.machine =
    { ... }:
    {
      imports = [
        nixosModule.nixosModules.default
        examples.partitionRepairDisabled
      ];
      services.postgresql.enable = true;
    };

  testScript = ''
    start_all()
    machine.wait_for_unit("postgresql.target")

    # Proves the value actually reached the real systemd unit's
    # environment, not just that the module evaluated.
    unit_environment = machine.succeed(
        "systemctl show postgresql-collation-guard.service --property=Environment --value"
    )
    assert "COLLATION_GUARD_PARTITION_REPAIR_ENABLE=false" in unit_environment, unit_environment
  '';
}
