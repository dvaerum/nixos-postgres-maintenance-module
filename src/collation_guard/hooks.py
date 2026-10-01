"""Hook loading, environment merging, and invocation.

Every hook point (preStart, onSuccess, onFailure, postRun, and the
perDatabase.* tier) goes through run_hook() -- including onFailure,
whose only difference from the rest is which process invokes it (a
second, --on-failure-triggered process, for crash-safety reasons; see
docs/decisions/0006). There is no stage-specific branching in this
module itself.
"""

from __future__ import annotations


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
