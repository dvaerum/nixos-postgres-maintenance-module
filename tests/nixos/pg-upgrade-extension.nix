# Heavy-tier coverage for the single most-cited real-world pg_upgrade
# failure mode: an extension installed on the old cluster
# (docs/decisions/0010's option docs explicitly call this out --
# "if the old cluster has extensions installed, pass the same
# .withPackages-wrapped package"). pgUpgradeTest's own old cluster has
# none at all. `citext` is a core contrib module bundled directly in
# nixpkgs's postgresql derivation (no .withPackages wrapping needed
# either side) with no shared_preload_libraries requirement -- a
# simple, reliable extension to prove the property under test:
# CREATE EXTENSION'd objects (and their actual runtime behavior, not
# just the catalog row surviving pg_dump/pg_restore) keep working
# after a real pg_upgrade.
#
# Not wired into `checks` -- same reasoning as pg-upgrade.nix. Run by
# hand via `nix build .#pgUpgradeExtensionTest -L`.
{
  postgresqlOld,
  postgresqlNew,
  nixosModule,
}:
{
  name = "collation-guard-pg-upgrade-extension";

  containers.machine =
    { lib, ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresql.package = postgresqlNew;
      services.postgresqlCollationGuard.enable = true;
      services.postgresqlCollationGuard.upgrade = {
        enable = true;
        oldPackage = postgresqlOld;
      };
      systemd.targets.postgresql.wantedBy = lib.mkForce [ ];
    };

  testScript = ''
    start_all()

    old_datadir = "/var/lib/postgresql/${postgresqlOld.psqlSchema}"
    pg_old = "${postgresqlOld}"

    machine.succeed("mkdir -p /var/lib/postgresql && chown postgres:postgres /var/lib/postgresql")
    machine.succeed(
        f"runuser -u postgres -- {pg_old}/bin/initdb --pgdata={old_datadir} "
        "--locale=en_US.UTF-8 --encoding=UTF8 --auth=trust --no-sync -U postgres"
    )
    machine.succeed(
        f"runuser -u postgres -- {pg_old}/bin/pg_ctl -D {old_datadir} "
        f"-l {old_datadir}.log -o '-c unix_socket_directories=/tmp' start"
    )
    machine.succeed(
        f"""runuser -u postgres -- {pg_old}/bin/psql -h /tmp -d postgres -U postgres """
        """-v ON_ERROR_STOP=1 """
        """-c "CREATE EXTENSION citext;" """
        """-c "CREATE TABLE ci_test (name citext);" """
        """-c "INSERT INTO ci_test VALUES ('Hello');" """
    )
    # Proves citext's own case-insensitive comparison operator actually
    # works on the OLD cluster, before migration -- not just that the
    # extension's catalog row exists.
    pre_migration_match = machine.succeed(
        """runuser -u postgres -- {}/bin/psql -h /tmp -d postgres -U postgres """
        """-tAc "SELECT name FROM ci_test WHERE name = 'hello';" """.format(pg_old)
    ).strip()
    assert pre_migration_match == "Hello", pre_migration_match

    machine.succeed(f"runuser -u postgres -- {pg_old}/bin/pg_ctl -D {old_datadir} -m fast stop")

    machine.succeed("systemctl start postgresql.target")
    machine.wait_for_unit("postgresql.target")

    upgrade_result = machine.succeed(
        "systemctl show postgresql-collation-guard-upgrade.service --property=Result --value"
    ).strip()
    assert upgrade_result == "success", upgrade_result

    # The extension's catalog entry survived the real pg_upgrade...
    extversion = machine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT extname FROM pg_extension WHERE extname = 'citext';" """
    ).strip()
    assert extversion == "citext", extversion

    # ...and, more importantly, its actual runtime behavior still works
    # under the NEW binary -- the real property pg_upgrade's own
    # extension-compatibility risk is about, not just a surviving catalog row.
    post_migration_match = machine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT name FROM ci_test WHERE name = 'hello';" """
    ).strip()
    assert post_migration_match == "Hello", post_migration_match
  '';
}
