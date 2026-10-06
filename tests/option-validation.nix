# Pins the NixOS eval-level option validation behavior for the
# upgrade.* option surface (docs/decisions/0010, 0011) -- confirmed
# correct by hand during a critical review (nix eval against a real
# constructed NixOS system), but previously unpinned by any actual
# test: a future refactor (e.g. giving oldPackage a spurious default,
# or moving the psqlSchema access somewhere no longer gated by
# `lib.mkIf cfg.upgrade.enable`) could silently regress any of these
# with nothing catching it.
#
# Full NixOS module evaluation (not validate-hook.nix's plain-data-in/
# plain-data-out style) since what's under test here is genuinely
# NixOS-specific: the `assertions` mechanism and option type system,
# not a pure function this project defines itself. Only `config`
# attributes narrow enough to force the specific checks under test are
# read -- never `config.system.build.toplevel` (which would pull in
# building the actual system closure, far more than an eval-level test
# needs).
{ pkgs, nixosModule }:
let
  inherit (pkgs) lib;

  baseConfig = {
    imports = [ nixosModule.nixosModules.default ];
    services.postgresql.enable = true;
    system.stateVersion = "24.05";
    fileSystems."/" = {
      device = "/dev/sda1";
      fsType = "ext4";
    };
    boot.loader.grub.device = "nodev";
  };

  evalWith =
    extraConfig:
    import "${pkgs.path}/nixos" {
      system = pkgs.system;
      configuration = lib.recursiveUpdate baseConfig extraConfig;
    };

  failedAssertions = eval: builtins.filter (a: !a.assertion) eval.config.assertions;

  # Case 1: upgrade.enable = true with the top-level enable left false
  # (docs/decisions/0011's own silent-no-op gap) must produce a failed
  # assertion naming both option paths.
  missingEnableFailed = failedAssertions (evalWith {
    services.postgresqlCollationGuard.enable = false;
    services.postgresqlCollationGuard.upgrade.enable = true;
    services.postgresqlCollationGuard.upgrade.oldPackage = pkgs.postgresql_16;
  });

  # Case 2: the normal, fully-correct configuration must produce ZERO
  # failed assertions -- the fix for case 1 must not be a false
  # positive that also rejects valid configs.
  okFailed = failedAssertions (evalWith {
    services.postgresqlCollationGuard.enable = true;
    services.postgresqlCollationGuard.upgrade.enable = true;
    services.postgresqlCollationGuard.upgrade.oldPackage = pkgs.postgresql_16;
  });

  # Case 3: oldPackage has no default -- upgrade.enable = true without
  # it set must throw (NixOS's own "used but not defined" mechanism),
  # naming the option path. Forces just the one systemd unit
  # definition that actually reads oldPackage.psqlSchema, not the
  # whole system closure.
  missingOldPackageResult = builtins.tryEval (
    builtins.deepSeq
      (evalWith {
        services.postgresqlCollationGuard.enable = true;
        services.postgresqlCollationGuard.upgrade.enable = true;
      }).config.systemd.services."postgresql-collation-guard-upgrade".environment
      true
  );

  # Case 4: an invalid transferMode must throw (NixOS's own enum type
  # check), not silently coerce to something else.
  badTransferModeResult = builtins.tryEval (
    builtins.deepSeq
      (evalWith {
        services.postgresqlCollationGuard.enable = true;
        services.postgresqlCollationGuard.upgrade.enable = true;
        services.postgresqlCollationGuard.upgrade.oldPackage = pkgs.postgresql_16;
        services.postgresqlCollationGuard.upgrade.transferMode = "teleport";
      }).config.systemd.services."postgresql-collation-guard-upgrade".environment
      true
  );

  allPass =
    missingEnableFailed != [ ]
    && okFailed == [ ]
    && !missingOldPackageResult.success
    && !badTransferModeResult.success;
in
# The assert wraps the whole returned expression -- same reasoning as
# validate-hook.nix's own comment: passthru.* is never forced by `nix
# build`/`nix flake check` unless something else reads it.
assert allPass;
pkgs.runCommand "option-validation-test" { } ''
  echo ok > "$out"
''
