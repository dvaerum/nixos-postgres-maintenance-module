{
  config,
  lib,
  pkgs,
  collationGuardPackage ? null,
  ...
}:
{
  imports = [
    ./options.nix
    ./config.nix
  ];

  config = lib.mkIf (collationGuardPackage != null) {
    services.postgresqlCollationGuard.package = lib.mkDefault collationGuardPackage;
  };
}
