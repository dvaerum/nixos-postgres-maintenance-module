# Opting out of connection lockdown -- only for a deployment with a
# specific reason to allow concurrent connections during the guard's
# run; see docs/decisions/0007-connection-lockdown-during-repair.md
# for the correctness gap this normally closes.
{ ... }:
{
  services.postgresqlCollationGuard = {
    enable = true;
    connectionLockdown.enable = false;
  };
}
