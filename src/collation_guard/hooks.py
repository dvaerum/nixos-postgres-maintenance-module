"""Hook loading, environment merging, and invocation.

Every hook point (preStart, onSuccess, onFailure, postRun, and the
perDatabase.* tier) goes through run_hook() -- including onFailure,
whose only difference from the rest is which process invokes it (a
second, --on-failure-triggered process, for crash-safety reasons; see
docs/decisions/0006). There is no stage-specific branching in this
module itself.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Hook:
    """One configured hook -- mirrors nixosModule/options.nix's
    hookType submodule exactly, field for field. block_on_failure is a
    plain bool, not Optional: Nix validates every hook has it set
    explicitly at eval time (see docs/decisions/0006), so by the time a
    Hook reaches Python it's never None."""

    path: str
    args: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    environment_file: str | None = None
    block_on_failure: bool = False


@dataclass(frozen=True, slots=True)
class HookResult:
    ok: bool
    block_on_failure: bool
    stdout: str
    stderr: str
    returncode: int


class EnvironmentCollisionError(Exception):
    """Raised when the same environment variable name is defined by
    more than one source for a single hook invocation. Deliberately
    strict: silently letting one source shadow another is exactly the
    kind of bug that's invisible until the wrong value reaches a hook
    in production (see docs/decisions/0006)."""


def merge_environment(*sources: dict[str, str]) -> dict[str, str]:
    """Merges environment dicts. Any key present in more than one
    source raises EnvironmentCollisionError -- never "last one wins,"
    regardless of whether the colliding values happen to be equal."""
    merged: dict[str, str] = {}
    for source in sources:
        for key, value in source.items():
            if key in merged:
                raise EnvironmentCollisionError(
                    f"environment variable {key!r} is defined by more than one source"
                )
            merged[key] = value
    return merged


def parse_environment_file(path: str) -> dict[str, str]:
    """Parses a systemd-EnvironmentFile-style KEY=VALUE file: blank
    lines and lines starting with '#' are ignored, and a value may be
    wrapped in matching single or double quotes. Not a full
    systemd-grammar parser (no line continuation, no shell-style
    escaping) -- covers the common case, including sops-nix secret
    files."""
    result: dict[str, str] = {}
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key] = value
    return result


def run_hook(
    hook: Hook,
    *,
    stage: str,
    database: str | None = None,
    context: dict[str, object] | None = None,
    error: str | None = None,
) -> HookResult:
    """Runs one hook, merging the stage's default vars with
    hook.environment_file and hook.environment (see merge_environment()
    for the collision rule -- a collision propagates out of this call
    as EnvironmentCollisionError, the hook never runs). The ambient
    process environment (PATH, HOME, PGHOST/PGPORT/etc.) is inherited
    underneath the merged set, not part of the collision check."""
    default_vars: dict[str, str] = {"COLLATION_GUARD_STAGE": stage}
    if database is not None:
        default_vars["COLLATION_GUARD_DATABASE"] = database
    if context is not None:
        default_vars["COLLATION_GUARD_CONTEXT"] = json.dumps(context)
    if error is not None:
        default_vars["COLLATION_GUARD_ERROR"] = error

    file_vars = parse_environment_file(hook.environment_file) if hook.environment_file else {}
    merged = merge_environment(default_vars, file_vars, hook.environment)

    result = subprocess.run(
        [hook.path, *hook.args],
        env={**os.environ, **merged},
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    if not ok:
        logger.warning(
            "hook %s (stage=%s) exited %d: %s",
            hook.path,
            stage,
            result.returncode,
            result.stderr,
        )
    return HookResult(
        ok=ok,
        block_on_failure=hook.block_on_failure,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
    )
