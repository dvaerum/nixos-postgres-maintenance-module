# nspawn test containers can't simulate a genuine crash

`tests/nixos/*.nix` uses the systemd-nspawn container backend, not
QEMU (ADR 0005). This project needed to prove a specific guarantee
(docs/decisions/0009): after a genuine machine-level crash -- nothing
graceful enough for the `OnFailure=` companion unit to ever fire --
the guard's own next run self-heals any stale `connectionLockdown`
state before doing new work. Simulating that crash turned out not to
be possible with this test backend, confirmed by actually trying it,
not assumed.

## What was tried

`BaseMachine.crash()` (QEMU: `send_monitor_command("quit")`) isn't
implemented for `NspawnMachine` at all -- no QEMU monitor for a
container. The next most direct idea: `os.kill(machine.process.pid, 9)`
-- SIGKILL the `systemd-nspawn` subprocess the test driver itself
launched (`self.process`, a plain `subprocess.Popen` handle) -- then
`machine.wait_for_shutdown()` (reaps it, clears `self.process`) and
`machine.start()` again against the same `state_dir`. This seemed like
it should work: more faithful than QEMU's own `crash()`, since nothing
inside the container gets any chance to run a shutdown sequence at
all.

## What actually happened

Confirmed via two independent signals, in that order:

1. `findmnt -no SOURCE,FSTYPE /run` showed the exact same `tmpfs
   tmpfs` both immediately before the kill and immediately after
   `machine.start()` returned and `postgresql.target` was reported
   active again. A genuine new boot mounts `/run` fresh every time
   (every Linux system using the FHS `/run` convention, container or
   not) -- an unchanged mount strongly suggested nothing had actually
   restarted.
2. Conclusive: `systemctl show postgresql-collation-guard.service
   --property=InvocationID` returned the *identical* ID before and
   after. `InvocationID` is systemd's own purpose-built "did this unit
   genuinely start again" signal -- a fresh one is assigned every
   single `ExecStart` invocation, unconditionally. Pulling the raw
   build log and diffing the two "boot sequences" the journal
   appeared to show made it unambiguous: the timestamps, PIDs, and the
   `Startup finished in 7.192s` line were byte-for-byte identical
   between the "before" and "after" excerpts -- one single boot,
   printed twice, not two boots.

Root cause (not fully chased down further, since it wasn't necessary
to -- see below): `machine.process` does not correspond to a process
whose death tears down the actual running container for this
backend's launch mechanism. The container likely keeps running
(reparented, detached, or registered independently of the tracked
`Popen` handle) regardless of what happens to that one PID, so the
"restart" was a no-op that simply reconnected to the still-running
instance and re-streamed its already-buffered journal output.

## Why this wasn't chased further

The cost/value tradeoff didn't justify it. The property actually at
risk here -- `/var/lib` surviving a reboot while `/run` doesn't -- is
not a project-specific behavior to independently verify; it's the
universal, foundational Linux/systemd `/run`-is-tmpfs convention
(true since ~2012, true on literally every FHS-compliant Linux
system, container or bare metal). What *is* project-specific and
worth testing directly is `_cleanup_stale_lockdown_state()`'s own
behavior -- and that's already proven, thoroughly, against a real
ephemeral Postgres cluster in `tests/test_main.py`
(`test_run_restores_a_stale_connection_limit_state_before_doing_new_work`
and its `pg_hba` sibling), which doesn't need a container reboot at
all: it just calls `main.run()` against a state file already sitting
on disk, exactly reproducing the state a genuine crash would leave
behind. Chasing the actual nspawn process-tree/lifecycle question
further (`machinectl terminate`? signaling a cgroup? a QEMU-backed
test instead?) would be a materially bigger lift than this fix's own
scope for a property that doesn't need re-proving.

## If this needs revisiting

A `virtualisation.vmVariant`/QEMU-backed test (real `crash()` support)
would close this properly, at the cost of a slower test and a new
test-backend dependency this project doesn't otherwise have. Worth it
only if a *different* guarantee -- one that's actually specific to
this project's own code, not a platform-wide convention -- ever needs
proving through an actual reboot.
