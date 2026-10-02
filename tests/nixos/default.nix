{ pkgs, nixosModule }:
{
  freshCluster = pkgs.testers.nixosTest (import ./fresh-cluster.nix { inherit nixosModule; });
  idempotent = pkgs.testers.nixosTest (import ./idempotent.nix { inherit nixosModule; });
  reindexFailure = pkgs.testers.nixosTest (import ./reindex-failure.nix { inherit nixosModule; });
  examples = pkgs.testers.nixosTest (import ./examples.nix { inherit nixosModule; });
  partitionRepairDisabled = pkgs.testers.nixosTest (
    import ./partition-repair-disabled.nix { inherit nixosModule; }
  );
  preStartHook = pkgs.testers.nixosTest (import ./pre-start-hook.nix { inherit nixosModule; });
  onFailureHook = pkgs.testers.nixosTest (import ./on-failure-hook.nix { inherit nixosModule; });
}
