{ pkgs, nixosModule }:
{
  # Populated one TDD cycle at a time -- see docs/decisions and the
  # project plan's "TDD implementation sequence". Kept as an empty
  # attrset (not omitted) so `flake.nix`'s `// nixosTests` always
  # evaluates to a real attrset, even before the first test exists.
}
