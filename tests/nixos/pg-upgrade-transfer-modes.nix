# Heavy-tier coverage for EXPLICIT transfer modes against a real
# pg_upgrade binary (docs/decisions/0010) -- resolve_transfer_mode()'s
# own preflight checks and pg_upgrade's own argv construction are
# already proven with mocked subprocess calls in tests/test_upgrade.py,
# but "copy" and "link" (chosen explicitly, bypassing "auto") had never
# been run against a real two-major-version migration. "clone" is
# deliberately NOT covered here -- it depends on the backing
# filesystem's own reflink/copy-on-write support, which a
# systemd-nspawn container's root (typically overlayfs/tmpfs) cannot
# reliably guarantee either way; already-correct empirical probing
# (reflink_supported()) is proven directly in the fast tier instead.
#
# Two machines, one build: "copy" needs no filesystem relationship
# between old/new datadir at all (the common, safest default); "link"
# is the one mode whose own docstring (upgrade.py) says a failure means
# silent corruption of the old cluster the moment the new one writes --
# worth proving it actually produces a working migration for real, not
# just the right argv.
#
# Not wired into `checks` -- same reasoning as pg-upgrade.nix. Run by
# hand via `nix build .#pgUpgradeTransferModesTest -L`.
{
  postgresqlOld,
  postgresqlNew,
  nixosModule,
}:
{
  name = "collation-guard-pg-upgrade-transfer-modes";

  containers.copyMachine =
    { lib, ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresql.package = postgresqlNew;
      services.postgresqlCollationGuard.enable = true;
      services.postgresqlCollationGuard.upgrade = {
        enable = true;
        oldPackage = postgresqlOld;
        transferMode = "copy";
      };
      systemd.targets.postgresql.wantedBy = lib.mkForce [ ];
    };

  containers.linkMachine =
    { lib, ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresql.package = postgresqlNew;
      services.postgresqlCollationGuard.enable = true;
      services.postgresqlCollationGuard.upgrade = {
        enable = true;
        oldPackage = postgresqlOld;
        transferMode = "link";
      };
      systemd.targets.postgresql.wantedBy = lib.mkForce [ ];
    };

  testScript = ''
    start_all()

    old_datadir = "/var/lib/postgresql/${postgresqlOld.psqlSchema}"
    new_datadir = "/var/lib/postgresql/${postgresqlNew.psqlSchema}"
    pg_old = "${postgresqlOld}"

    def seed_and_migrate(machine, expected_flag):
        machine.succeed(
            "mkdir -p /var/lib/postgresql && chown postgres:postgres /var/lib/postgresql"
        )
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
            """-v ON_ERROR_STOP=1 -c "CREATE TABLE upgrade_probe (id int, note text);" """
            f"""-c "INSERT INTO upgrade_probe VALUES (1, '{expected_flag}-migrated');" """
        )
        machine.succeed(f"runuser -u postgres -- {pg_old}/bin/pg_ctl -D {old_datadir} -m fast stop")

        machine.succeed("systemctl start postgresql.target")
        machine.wait_for_unit("postgresql.target")

        upgrade_result = machine.succeed(
            "systemctl show postgresql-collation-guard-upgrade.service --property=Result --value"
        ).strip()
        assert upgrade_result == "success", upgrade_result

        row = machine.succeed(
            """runuser -u postgres -- psql -tAc "SELECT note FROM upgrade_probe WHERE id = 1;" """
        ).strip()
        assert row == f"{expected_flag}-migrated", row

    seed_and_migrate(copyMachine, "copy")
    seed_and_migrate(linkMachine, "link")

    # "link" hard-links old and new cluster relation files together --
    # confirm that's really what happened (not silently resolved to
    # something else, and not just "the migration worked, who knows
    # how"). pg_upgrade preserves relfilenodes across versions
    # specifically so --link/--clone can do this, so the migrated
    # table's own physical file under the NEW cluster is the thing to
    # check -- PG_VERSION itself legitimately differs between old (16)
    # and new (17) and could never be hard-linked either way.
    relfilepath = linkMachine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT pg_relation_filepath('upgrade_probe');" """
    ).strip()
    link_count = linkMachine.succeed(f"stat -c %h {new_datadir}/{relfilepath}").strip()
    assert int(link_count) >= 2, (
        f"expected {new_datadir}/{relfilepath} to be hard-linked to the old "
        f"cluster's own copy (link count >= 2) under --link, got {link_count}"
    )
  '';
}
