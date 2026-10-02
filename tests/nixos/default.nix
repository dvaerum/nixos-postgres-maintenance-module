{ pkgs, nixosModule }:
{
  freshCluster = pkgs.testers.nixosTest (import ./fresh-cluster.nix { inherit nixosModule; });
  idempotent = pkgs.testers.nixosTest (import ./idempotent.nix { inherit nixosModule; });
  reindexFailure = pkgs.testers.nixosTest (import ./reindex-failure.nix { inherit nixosModule; });
  examples = pkgs.testers.nixosTest (import ./examples.nix { inherit nixosModule; });
}
