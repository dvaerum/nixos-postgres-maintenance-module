# Cycle 15: blockOnFailure's forced choice. Tests
# nixosModule/validate-hook.nix in complete isolation -- plain data in,
# plain data (or a throw) out -- rather than a full NixOS module
# evaluation, since the function itself has no NixOS-specific
# dependencies at all.
{ pkgs }:
let
  validateHook = import ../nixosModule/validate-hook.nix;

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

  allPass =
    builtins.all throwsOnMissingBlockOnFailure optionPaths
    && builtins.all passesThroughWhenSet optionPaths;
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
