# Cycle 15: blockOnFailure's forced choice. Cycle (round 2, card 2):
# a hook's `environment` must not collide with a var this stage
# always sets itself (src/collation_guard/hooks.py's run_hook()
# default_vars) -- same mistake that used to crash the whole run at
# runtime (see docs/decisions/0006), now caught here instead. Tests
# nixosModule/validate-hook.nix in complete isolation -- plain data in,
# plain data (or a throw) out -- rather than a full NixOS module
# evaluation, since the function itself has no NixOS-specific
# dependencies at all.
{ pkgs }:
let
  validateHook = import ../nixosModule/validate-hook.nix { inherit (pkgs) lib; };

  hookMissingBlockOnFailure = {
    path = "/bin/true";
    args = [ ];
    environment = { };
    environmentFile = null;
    blockOnFailure = null; # the type default -- deliberately invalid
  };
  hookWithBlockOnFailure = hookMissingBlockOnFailure // {
    blockOnFailure = true;
  };

  # Every hook list this project actually declares in options.nix --
  # the validation needs to be proven for each one, not assumed from a
  # single representative case.
  optionPaths = [
    "preStart"
    "onSuccess"
    "onFailure"
    "postRun"
    "perDatabase.preStart"
    "perDatabase.onSuccess"
    "perDatabase.onFailure"
  ];

  throwsOnMissingBlockOnFailure =
    optionPath:
    let
      result = builtins.tryEval (
        builtins.deepSeq (validateHook optionPath hookMissingBlockOnFailure) true
      );
    in
    if result.success then
      throw "validate-hook: expected ${optionPath} to throw when blockOnFailure is unset, but it didn't"
    else
      true;

  # A hook that already sets blockOnFailure must pass straight through
  # unchanged -- validation shouldn't itself reject or mutate a
  # correctly-configured hook.
  passesThroughWhenSet =
    optionPath:
    if (validateHook optionPath hookWithBlockOnFailure) != hookWithBlockOnFailure then
      throw "validate-hook: ${optionPath} changed a correctly-configured hook"
    else
      true;

  # The reserved vars run_hook() (src/collation_guard/hooks.py)
  # injects for each stage -- STAGE is universal, DATABASE only for
  # perDatabase.*, ERROR only for the three failure-reporting stages
  # (onFailure, postRun, perDatabase.onFailure). Kept as an explicit,
  # independent table here (not reused from
  # validate-hook.nix's own implementation) so a bug in that
  # function's own mapping can't also hide from this test.
  allReservedVars = [
    "COLLATION_GUARD_STAGE"
    "COLLATION_GUARD_DATABASE"
    "COLLATION_GUARD_CONTEXT"
    "COLLATION_GUARD_ERROR"
  ];
  reservedVarsByPath = {
    preStart = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_CONTEXT"
    ];
    onSuccess = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_CONTEXT"
    ];
    onFailure = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_CONTEXT"
      "COLLATION_GUARD_ERROR"
    ];
    postRun = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_CONTEXT"
      "COLLATION_GUARD_ERROR"
    ];
    "perDatabase.preStart" = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_DATABASE"
      "COLLATION_GUARD_CONTEXT"
    ];
    "perDatabase.onSuccess" = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_DATABASE"
      "COLLATION_GUARD_CONTEXT"
    ];
    "perDatabase.onFailure" = [
      "COLLATION_GUARD_STAGE"
      "COLLATION_GUARD_DATABASE"
      "COLLATION_GUARD_CONTEXT"
      "COLLATION_GUARD_ERROR"
    ];
  };

  hookWithEnv =
    env:
    hookMissingBlockOnFailure
    // {
      environment = env;
      blockOnFailure = true;
    };

  throwsWhen = pkgs.lib.unique (builtins.concatLists (builtins.attrValues reservedVarsByPath));

  throwsOnEveryReservedVarForItsOwnPath =
    optionPath:
    builtins.all (
      var:
      let
        result = builtins.tryEval (
          builtins.deepSeq (validateHook optionPath (hookWithEnv {
            ${var} = "x";
          })) true
        );
      in
      if result.success then
        throw "validate-hook: expected ${optionPath} to throw on reserved var ${var}, but it didn't"
      else
        true
    ) reservedVarsByPath.${optionPath};

  # A reserved var that does NOT apply to this particular path (e.g.
  # COLLATION_GUARD_ERROR for a plain preStart) must pass through --
  # proves the check is scoped per-path, not a blanket reject of all
  # four names everywhere.
  doesNotThrowOnVarsOutsideItsOwnPath =
    optionPath:
    let
      irrelevant = builtins.filter (v: !(builtins.elem v reservedVarsByPath.${optionPath})) throwsWhen;
    in
    builtins.all (
      var:
      (validateHook optionPath (hookWithEnv {
        ${var} = "x";
      })) == (hookWithEnv { ${var} = "x"; })
    ) irrelevant;

  # A legitimate, non-reserved custom var must never be rejected.
  passesThroughCustomVar =
    optionPath:
    (validateHook optionPath (hookWithEnv {
      MY_CUSTOM_VAR = "x";
    })) == (hookWithEnv { MY_CUSTOM_VAR = "x"; });

  allPass =
    builtins.all throwsOnMissingBlockOnFailure optionPaths
    && builtins.all passesThroughWhenSet optionPaths
    && builtins.all throwsOnEveryReservedVarForItsOwnPath optionPaths
    && builtins.all doesNotThrowOnVarsOutsideItsOwnPath optionPaths
    && builtins.all passesThroughCustomVar optionPaths;
in
# The assert wraps the whole returned expression, not a passthru
# attribute -- passthru.* is never forced by `nix build`/`nix flake
# check` unless something else reads it, which would make a failing
# assertion placed there a silent no-op instead of a real check
# failure. Wrapping the outer expression forces it as soon as this
# file is evaluated at all.
assert allPass;
pkgs.runCommand "validate-hook-test" { } ''
  echo ok > "$out"
''
