# AGENTS.md

Instructions for any coding agent (human or AI) working in this repo.

## What this is

A NixOS module + Python CLI that closes
[nixpkgs#318777](https://github.com/NixOS/nixpkgs/issues/318777): it
reindexes/refreshes a Postgres cluster whose collation library version
drifted, and repairs text-partition bounds after a collation change,
before `postgresql.target` ever comes up. See the plan/ADRs in
`docs/decisions/` for the full reasoning behind each design choice —
don't re-derive decisions already recorded there.

## Project structure

```
flake.nix          nixosModules.default, packages.default, devShells.default, checks
pyproject.toml     single source of truth for the Python package (see "Versioning" below)
src/collation_guard/   the package: main.py (entry point), collation.py, partitions.py,
                    hooks.py, lockdown.py
tests/              fast tier: pytest + ephemeral initdb/pg_ctl cluster (no systemd)
tests/nixos/        slow tier: systemd-nspawn nixosTest containers (wiring/ordering,
                    plus icu-drift.nix -- a real multi-Postgres-build drift test,
                    exposed as packages.icuDriftTest, not wired into `checks`/CI)
nixosModule/        services.postgresqlCollationGuard.* options + systemd unit wiring
docs/decisions/      one ADR per real design decision, with sources cited
docs/learnings/       cross-cutting operational knowledge, not tied to one decision
```

## Workflow

- TDD, red/green/refactor: write the failing test first, smallest code
  to pass, then refactor. Don't write implementation ahead of its test.
- Two test tiers — put logic tests in `tests/*.py` (fast, runs
  constantly); only use `tests/nixos/*.nix` for things that genuinely
  need a real systemd unit chain (does a failure block
  `postgresql.target`, does option wiring reach the service).
- `nix develop` for a complete local loop (`python3`, `psycopg`,
  `pytest`, `ruff`, `mypy`, `postgresql` all on `PATH`).
- Gate before committing: `pytest`, `ruff check .`, `mypy`, and
  `nix flake check` (runs the Nix-level build + both test tiers).
- `nix fmt` formats Nix only (`nixfmt-rfc-style`, flake.nix's sole
  `formatter`) -- there's no Python formatter wired in; `ruff check .`
  in the gate above only lints, it doesn't reformat.

## Versioning

[Semantic Versioning 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — a breaking change to `services.postgresqlCollationGuard.*`
  options, the CLI's invocation/exit-code contract, or anything else a
  consumer could depend on.
- **MINOR** — backwards-compatible functionality (a new option, a new
  repair capability).
- **PATCH** — backwards-compatible bug fixes only.

**Single source of truth: `pyproject.toml`'s `[project] version`.**
Bump it there and nowhere else:

- `flake.nix` reads `pname`/`version` straight out of `pyproject.toml`
  via `builtins.fromTOML`, so the Nix package version can never drift
  out of sync with the Python package version.
- `collation_guard.__version__` (`src/collation_guard/__init__.py`)
  reads it back via `importlib.metadata.version("collation-guard")` --
  the value hatchling wrote into the installed package's metadata from
  `pyproject.toml` at build time, not a second literal. Falls back to
  `"0+unknown"` when running from a source checkout that was never
  `pip install -e .`'d (e.g. the test suite, which imports straight off
  `pythonpath`).

Never hardcode a version string anywhere else.

One version bump per release commit — don't bundle a version bump with
an unrelated change.

## Documentation

- Record a real design decision as a new, numbered ADR in
  `docs/decisions/NNNN-<title>.md` — decision, alternatives considered,
  WHY with sources, not a changelog.
- Record cross-cutting operational knowledge (a gotcha, a known
  limitation, something future-you would otherwise rediscover the hard
  way) in `docs/learnings/<topic>.md`.
- Don't duplicate information across docs — link to the canonical home
  instead of copying.
