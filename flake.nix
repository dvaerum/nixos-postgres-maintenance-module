{
  description = "NixOS module + CLI that closes nixpkgs#318777: reindex/refresh a Postgres cluster whose collation library version drifted, and repair text-partition bounds after a collation change, before postgresql.target ever comes up.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
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

        collation-guard = pkgs.python3Packages.buildPythonApplication {
          pname = "collation-guard";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = with pkgs.python3Packages; [ hatchling ];
          dependencies = with pkgs.python3Packages; [ psycopg ];

          nativeCheckInputs = with pkgs.python3Packages; [
            pytestCheckHook
            pkgs.postgresql
          ];

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
