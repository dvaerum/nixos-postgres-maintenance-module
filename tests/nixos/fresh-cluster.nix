# Cycle 0 (scaffold smoke: module wired, postgresql.target reaches
# active) folded into cycle 1 (fresh cluster writes the C.UTF-8 stamp
# on first boot) -- both need the same single fresh boot, no reason to
# pay for two separate container boots.
#
# systemd-nspawn container backend (not a QEMU VM): confirmed safe for
# services.postgresql by nixpkgs' own CI (nixos/tests/hedgedoc.nix runs
# the same Type=notify unit + UNIX socket this way). `sudo` does not
# work inside the container (confirmed open constraint from the
# feature's own PR discussion) -- `runuser -u postgres --` is used
# instead throughout.
{ nixosModule }:
{
  name = "collation-guard-fresh-cluster";

  containers.machine =
    { ... }:
    {
      imports = [ nixosModule.nixosModules.default ];
      services.postgresql.enable = true;
      services.postgresqlCollationGuard.enable = true;
    };

  testScript = ''
    start_all()

    # Cycle 0: the module is wired correctly and the whole chain
    # (postgresql-collation-guard -> postgresql-setup -> postgresql.target)
    # actually reaches active on a real boot -- if the guard's ExecStart
    # failed, postgresql-setup (which requires+after it) and
    # postgresql.target would never come up, so this alone already
    # proves the ordering/wiring, not just that the guard ran.
    machine.wait_for_unit("postgresql.target")
    machine.wait_for_unit("postgresql-collation-guard.service")

    # Cycle 1: the glibc locale store path actually substituted into
    # the unit (config.i18n.glibcLocales) reached the script and got
    # written as the stamp.
    stamp = machine.succeed(
        """runuser -u postgres -- psql -tAc "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = 'postgres';" """
    ).strip()
    assert stamp.startswith(
        "collation-guard:glibcLocales=/nix/store/"
    ), f"unexpected stamp: {stamp!r}"
  '';
}
