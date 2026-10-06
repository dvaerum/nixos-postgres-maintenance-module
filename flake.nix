{
  description = "NixOS module + CLI that closes nixpkgs#318777: reindex/refresh a Postgres cluster whose collation library version drifted, and repair text-partition bounds after a collation change, before postgresql.target ever comes up.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      utils,
    }:
    {
      nixosModules.default =
        { pkgs, ... }:
        {
          imports = [ ./nixosModule ];
          config._module.args.collationGuardPackage =
            self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
    }
    // utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };

        # pname/version read straight from pyproject.toml -- the one
        # source of truth (see AGENTS.md "Versioning"), so a release
        # only ever bumps one line.
        pyprojectToml = builtins.fromTOML (builtins.readFile ./pyproject.toml);

        collation-guard = pkgs.python3Packages.buildPythonApplication {
          pname = pyprojectToml.project.name;
          version = pyprojectToml.project.version;
          pyproject = true;

          src = ./.;

          build-system = with pkgs.python3Packages; [ hatchling ];
          dependencies = with pkgs.python3Packages; [ psycopg ];

          nativeCheckInputs =
            with pkgs.python3Packages;
            [
              pytestCheckHook
              pkgs.postgresql
            ]
            ++ pkgs.lib.optional pkgs.stdenv.isLinux pkgs.glibcLocales;

          # The Nix build sandbox ships no system locales at all -- without
          # this, initdb can't create a database with a real libc locale
          # (e.g. en_US.UTF-8), which the cycle-3 tests need: C/POSIX are
          # never versioned by Postgres at all, so they can't exercise the
          # datcollversion-mismatch path this package detects. glibcLocales
          # is Linux-only; darwin uses the host's own libc locales.
          env = pkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
            LOCALE_ARCHIVE = "${pkgs.glibcLocales}/lib/locale/locale-archive";
          };

          meta = with pkgs.lib; {
            description = "Postgres collation-drift guard for NixOS";
            homepage = "https://github.com/dvaerum/nixos-postgres-maintenance-module";
            license = licenses.mit;
            maintainers = [ ];
            mainProgram = "collation-guard";
          };
        };

        nixosTests = import ./tests/nixos {
          inherit pkgs;
          nixosModule = self;
        };

        # Two full Postgres builds, each linked against a different ICU
        # release -- the permanent form of the ICU-drift experiment (see
        # docs/decisions/0008). Only the icu input differs; everything
        # else about the build is the standard nixpkgs postgresql_17.
        postgresqlIcu72 = pkgs.postgresql_17.override { icu = pkgs.icu72; };
        postgresqlIcu73 = pkgs.postgresql_17.override { icu = pkgs.icu73; };

        icuDriftTest = pkgs.testers.nixosTest (
          import ./tests/nixos/icu-drift.nix {
            inherit postgresqlIcu72 postgresqlIcu73;
            collationGuardPackage = collation-guard;
          }
        );

        # A real PostgreSQL major-version migration across two genuinely
        # different binaries (docs/decisions/0010, 0011) -- both ordinary,
        # already-cached nixpkgs postgresql_NN derivations, unlike
        # icuDriftTest's custom ICU override rebuild above.
        pgUpgradeTest = pkgs.testers.nixosTest (
          import ./tests/nixos/pg-upgrade.nix {
            postgresqlOld = pkgs.postgresql_16;
            postgresqlNew = pkgs.postgresql_17;
            nixosModule = self;
          }
        );

        # A FAILED upgrade must block postgresql.service from starting
        # at all -- the one thing pgUpgradeTest's happy path can never
        # prove on its own.
        pgUpgradeFailureTest = pkgs.testers.nixosTest (
          import ./tests/nixos/pg-upgrade-failure.nix {
            postgresqlOld = pkgs.postgresql_16;
            postgresqlNew = pkgs.postgresql_17;
            nixosModule = self;
          }
        );

        # Explicit "copy"/"link" transfer modes against a real
        # pg_upgrade binary -- pgUpgradeTest above only exercises
        # whatever "auto" resolves to.
        pgUpgradeTransferModesTest = pkgs.testers.nixosTest (
          import ./tests/nixos/pg-upgrade-transfer-modes.nix {
            postgresqlOld = pkgs.postgresql_16;
            postgresqlNew = pkgs.postgresql_17;
            nixosModule = self;
          }
        );

        # An extension installed on the old cluster (the single
        # most-cited real-world pg_upgrade failure mode) surviving a
        # real migration, both its catalog entry and its actual
        # runtime behavior.
        pgUpgradeExtensionTest = pkgs.testers.nixosTest (
          import ./tests/nixos/pg-upgrade-extension.nix {
            postgresqlOld = pkgs.postgresql_16;
            postgresqlNew = pkgs.postgresql_17;
            nixosModule = self;
          }
        );

        # The positive-retention cleanup timer/service pair, against a
        # real completed-upgrade state file (backdated to simulate the
        # window elapsing, rather than waiting a literal day).
        pgUpgradeRetentionTimerTest = pkgs.testers.nixosTest (
          import ./tests/nixos/pg-upgrade-retention-timer.nix {
            postgresqlOld = pkgs.postgresql_16;
            postgresqlNew = pkgs.postgresql_17;
            nixosModule = self;
          }
        );
      in
      {
        packages.default = collation-guard;

        # Deliberately NOT a `checks` entry -- see docs/decisions/0008 for
        # why this heavy, multi-Postgres-rebuild tier stays out of
        # `nix flake check`/CI and is run by hand instead:
        #   nix build .#icuDriftTest -L
        packages.icuDriftTest = icuDriftTest;

        # Same reasoning as icuDriftTest above (a real multi-minute
        # two-cluster migration, not a logic-level check) -- run by hand:
        #   nix build .#pgUpgradeTest -L
        packages.pgUpgradeTest = pgUpgradeTest;
        packages.pgUpgradeFailureTest = pgUpgradeFailureTest;
        packages.pgUpgradeTransferModesTest = pgUpgradeTransferModesTest;
        packages.pgUpgradeExtensionTest = pgUpgradeExtensionTest;
        packages.pgUpgradeRetentionTimerTest = pgUpgradeRetentionTimerTest;

        checks = {
          unitTests = collation-guard;
          validateHook = import ./tests/validate-hook.nix { inherit pkgs; };
          optionValidation = import ./tests/option-validation.nix {
            inherit pkgs;
            nixosModule = self;
          };
        }
        // nixosTests;

        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            python3Packages.psycopg
            python3Packages.pytest
            ruff
            mypy
            postgresql
          ];
        };

        formatter = pkgs.nixfmt-rfc-style;
      }
    );
}
