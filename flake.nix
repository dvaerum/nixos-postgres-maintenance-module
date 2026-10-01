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
      in
      {
        packages.default = collation-guard;

        checks = {
          unitTests = collation-guard;
          validateHook = import ./tests/validate-hook.nix { inherit pkgs; };
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
