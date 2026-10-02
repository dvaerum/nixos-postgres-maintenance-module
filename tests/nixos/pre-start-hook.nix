# hooks.preStart + blockOnFailure = true: the one hook point that
# aborts the entire run before any database is ever examined (see the
# option's own description and docs/decisions/0006). Proves both
# halves of that claim: the guard's own unit reports "failed", and --
# the part a marker file alone can't show -- the context file written
# afterward records zero databases processed, not just a per-database
# failure somewhere downstream.
{ nixosModule }:
let
  examples = import ../../examples;
in
{
  name = "collation-guard-pre-start-hook";

  containers.machine =
    { ... }:
    {
      imports = [
        nixosModule.nixosModules.default
        examples.preStartHook
      ];
      services.postgresql.enable = true;
    };

  testScript = ''
    start_all()
    machine.wait_for_unit("multi-user.target")

    # The preStart hook always exits 1, so even the very first boot's
    # automatic attempt at postgresql.target already failed -- this
    # re-attempt is just to pin down a known-settled state before
    # asserting. Expected to fail -- capture via execute(), not succeed().
    _, _ = machine.execute("systemctl start postgresql.target")

    guard_state = machine.succeed(
        "systemctl is-active postgresql-collation-guard.service || true"
    ).strip()
    assert guard_state == "failed", f"expected the guard unit to be 'failed', got {guard_state!r}"

    marker = machine.succeed("cat /tmp/collation-guard-prestart-marker")
    assert "STAGE=pre_start" in marker, marker

    context = machine.succeed("cat /run/postgresql-collation-guard/last-run.json")
    assert '"databases_processed": []' in context, context
  '';
}
