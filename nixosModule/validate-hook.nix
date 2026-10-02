# Pure function: validates one hook has blockOnFailure explicitly set,
# and that its `environment` doesn't collide with a var this stage
# always sets itself. Extracted from config.nix so it's testable in
# isolation, without a full NixOS module evaluation -- see
# tests/validate-hook.nix and docs/decisions/0006 for why
# blockOnFailure has no default (the type default, null, is
# deliberately invalid at use).
{ lib }:
let
  # Mirrors run_hook()'s default_vars (src/collation_guard/hooks.py):
  # STAGE is set for every invocation; DATABASE only for perDatabase.*
  # stages; ERROR only for the three failure-reporting stages (onFailure,
  # postRun, perDatabase.onFailure). Keep this in lockstep with that
  # function if a new default var is ever added.
  reservedVarsFor =
    optionPath:
    [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_CONTEXT"
    ]
    ++ lib.optional (lib.hasPrefix "perDatabase." optionPath) "COLLATION_GUARD_DATABASE"
    ++ lib.optional (
      optionPath == "onFailure" || optionPath == "postRun" || optionPath == "perDatabase.onFailure"
    ) "COLLATION_GUARD_ERROR";
in
optionPath: hook:
if hook.blockOnFailure == null then
  throw ''
    services.postgresqlCollationGuard.hooks.${optionPath}: every hook must set
    blockOnFailure explicitly (true or false) -- got null (the unset default).
  ''
else
  let
    collisions = builtins.filter (k: builtins.elem k (reservedVarsFor optionPath)) (
      builtins.attrNames hook.environment
    );
  in
  if collisions != [ ] then
    throw ''
      services.postgresqlCollationGuard.hooks.${optionPath}: environment sets
      ${builtins.concatStringsSep ", " collisions}, which this stage always sets
      itself -- remove it from `environment` (see docs/decisions/0006 for why a
      collision is a hard error, not "last one wins").
    ''
  else
    hook
