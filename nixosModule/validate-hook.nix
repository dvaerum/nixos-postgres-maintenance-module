# Pure function: validates one hook has blockOnFailure explicitly set.
# Extracted from config.nix so it's testable in isolation, without a
# full NixOS module evaluation -- see tests/validate-hook.nix and
# docs/decisions/0006 for why blockOnFailure has no default (the type
# default, null, is deliberately invalid at use).
optionPath: hook:
if hook.blockOnFailure == null then
  throw ''
    services.postgresqlCollationGuard.hooks.${optionPath}: every hook must set
    blockOnFailure explicitly (true or false) -- got null (the unset default).
  ''
else
  hook
