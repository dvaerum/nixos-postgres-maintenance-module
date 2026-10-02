# 0001: Python, not bash

## Decision

Implement the guard as a Python package (`src/collation_guard/`), not
a shell script, packaged via `pkgs.python3Packages.buildPythonApplication`
reading `pyproject.toml` as the single source of truth.

## Alternatives considered

**Keep extending the existing bash script.** The guard this project
replaces (`collation-guard.sh` in `nixos-developer-system`) was already
built, deployed, and live-verified in bash. Extending it with the
partition-repair feature would have kept one less language in the
toolchain.

## Why

The bash version was already hitting a real complexity ceiling before
partition repair was even added:

- **Error discrimination requires parsing `psql` output as text.**
  Distinguishing "REINDEX failed because of a genuine duplicate key"
  from "REINDEX failed because of something else entirely" means
  grepping stderr for a substring, not catching a typed exception.
  Partition repair needs exactly this kind of discrimination
  (`psycopg.errors.CheckViolation` -- SQLSTATE `23514`,
  https://www.postgresql.org/docs/17/errcodes-appendix.html -- for an
  ATTACH-bounds violation vs. anything else), which bash has no real
  mechanism for beyond more string matching.
- **No structured control flow for retry/backoff loops.** Cycle 10's
  safety-capped repair loop, and cycle 7's per-row retry, are both
  naturally expressed as Python control flow with typed return values
  (`DatabaseResult`, `PartitionRepairResult`) -- the bash equivalent
  would be exit-code juggling and `$?` checks threaded through nested
  loops.
- **Precedent already exists in this fleet.** `storage_guard.py` (in
  the sibling `home-manager-config`/`celler` codebase) made the same
  move off bash for the same reason, packaged via
  `pkgs.writePython3Bin`. This project doesn't reuse that exact
  packaging (see below) but follows the same underlying judgment.

## Packaging: src-layout + `pyproject.toml`, not `writePython3Bin`

`writePython3Bin` fits a single-file script. This package has multiple
modules (`main.py`, `collation.py`, `partitions.py`) plus its own test
suite and needs real tooling (`pytest`, `mypy --strict`, `ruff`) wired
to it -- only a real `pyproject.toml`-based package gives that cleanly,
with one source of truth for both `pip install -e .` local development
and the Nix build (`flake.nix` reads `pname`/`version` straight out of
`pyproject.toml` via `builtins.fromTOML` -- see `AGENTS.md`'s
versioning section).
