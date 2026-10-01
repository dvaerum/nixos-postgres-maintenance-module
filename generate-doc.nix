# Just run this nix program with: nix-build generate-doc.nix
# Same pattern as github:dvaerum/nixos-router-module's own generate-doc.nix.

{
  pkgs ? import <nixpkgs> { },
  ...
}:
let
  inherit (pkgs) lib nixosOptionsDoc runCommand;

  eval = lib.evalModules {
    modules = [ ./nixosModule/options.nix ];
  };
  optionsDoc = nixosOptionsDoc {
    inherit (eval) options;
  };
in
runCommand "options-doc.md" { } ''
  cat ${optionsDoc.optionsCommonMark} >> $out
''
