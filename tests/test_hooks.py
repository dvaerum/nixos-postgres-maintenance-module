"""Fast tier: hook loading, environment merging, and invocation.

New in the hooks redesign -- see docs/decisions/0006 for why every hook
(including the systemd-triggered onFailure one) goes through this same
code, and why a collision between environment sources is a hard error,
never a silent override.
"""

from __future__ import annotations

import json
import sys

import pytest

from collation_guard.hooks import (
    EnvironmentCollisionError,
    Hook,
    load_hooks,
    merge_environment,
    parse_environment_file,
    run_hook,
)

_RECORDER = """
import json, os, sys
with open(sys.argv[1], "w") as f:
    json.dump({"argv": sys.argv[2:], "env": dict(os.environ)}, f)
"""


def _make_recorder(tmp_path):
    """A real subprocess (not mocked) that records its own argv/env to
    a file -- proves run_hook()'s plumbing, not just that
    subprocess.run was called."""
    script = tmp_path / "recorder.py"
    script.write_text(_RECORDER)
    out = tmp_path / "recorded.json"
    return script, out


def test_parse_environment_file_basic(tmp_path):
    path = tmp_path / "env"
    path.write_text("FOO=bar\nBAZ=qux\n")
    assert parse_environment_file(str(path)) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_environment_file_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "env"
    path.write_text("# a comment\n\nFOO=bar\n\n# another\nBAZ=qux\n")
    assert parse_environment_file(str(path)) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_environment_file_unwraps_quoted_values(tmp_path):
    path = tmp_path / "env"
    path.write_text('SINGLE=\'hello world\'\nDOUBLE="hello world"\nUNQUOTED=plain\n')
    assert parse_environment_file(str(path)) == {
        "SINGLE": "hello world",
        "DOUBLE": "hello world",
        "UNQUOTED": "plain",
    }


def test_parse_environment_file_empty_file(tmp_path):
    path = tmp_path / "env"
    path.write_text("")
    assert parse_environment_file(str(path)) == {}


def test_merge_environment_merges_disjoint_sources():
    merged = merge_environment({"A": "1"}, {"B": "2"}, {"C": "3"})
    assert merged == {"A": "1", "B": "2", "C": "3"}


def test_merge_environment_raises_on_collision_between_first_two_sources():
    with pytest.raises(EnvironmentCollisionError) as exc_info:
        merge_environment({"A": "1"}, {"A": "2"}, {"B": "3"})
    assert "A" in str(exc_info.value)


def test_merge_environment_raises_even_when_colliding_values_are_equal():
    """Equal values still raise -- the rule is "key appears in more
    than one source," not "the values actually disagree." Explicit is
    safer than assuming a coincidental match means no real conflict."""
    with pytest.raises(EnvironmentCollisionError):
        merge_environment({"A": "1"}, {"A": "1"})


def test_merge_environment_raises_on_collision_between_any_pair_of_sources():
    with pytest.raises(EnvironmentCollisionError):
        merge_environment({"A": "1"}, {"B": "2"}, {"A": "3"})


def test_merge_environment_with_no_sources_is_empty():
    assert merge_environment() == {}


def test_merge_environment_with_one_source_passes_through():
    assert merge_environment({"A": "1"}) == {"A": "1"}


def test_run_hook_happy_path_passes_argv_and_merged_env(tmp_path):
    script, out = _make_recorder(tmp_path)
    hook = Hook(
        path=sys.executable,
        args=[str(script), str(out), "extra-arg"],
        environment={"FROM_ENVIRONMENT": "inline"},
    )

    result = run_hook(hook, stage="pre_start")

    assert result.ok
    recorded = json.loads(out.read_text())
    assert recorded["argv"] == ["extra-arg"]
    assert recorded["env"]["COLLATION_GUARD_STAGE"] == "pre_start"
    assert recorded["env"]["FROM_ENVIRONMENT"] == "inline"
    # ambient environment (e.g. PATH) is inherited, not stripped
    assert "PATH" in recorded["env"]


def test_run_hook_merges_environment_file_too(tmp_path):
    script, out = _make_recorder(tmp_path)
    env_file = tmp_path / "secrets.env"
    env_file.write_text("FROM_FILE=shh\n")
    hook = Hook(path=sys.executable, args=[str(script), str(out)], environment_file=str(env_file))

    run_hook(hook, stage="pre_start")

    recorded = json.loads(out.read_text())
    assert recorded["env"]["FROM_FILE"] == "shh"


def test_run_hook_collision_between_environment_and_environment_file_raises(tmp_path):
    script, out = _make_recorder(tmp_path)
    env_file = tmp_path / "secrets.env"
    env_file.write_text("DUPLICATE=from-file\n")
    hook = Hook(
        path=sys.executable,
        args=[str(script), str(out)],
        environment={"DUPLICATE": "from-inline"},
        environment_file=str(env_file),
    )

    with pytest.raises(EnvironmentCollisionError):
        run_hook(hook, stage="pre_start")


def test_run_hook_failure_path_does_not_raise(tmp_path):
    """A failing hook is reported via HookResult.ok, never an
    exception -- the caller (main.py) decides what a failure means per
    stage/block_on_failure, run_hook() itself just reports facts."""
    failing_script = tmp_path / "fail.py"
    failing_script.write_text("import sys; sys.stderr.write('boom'); sys.exit(1)\n")
    hook = Hook(path=sys.executable, args=[str(failing_script)], block_on_failure=True)

    result = run_hook(hook, stage="pre_start")

    assert not result.ok
    assert result.returncode == 1
    assert "boom" in result.stderr
    assert result.block_on_failure is True


def _hook_json(path: str = "/bin/true", block_on_failure: bool = True) -> dict:
    return {
        "path": path,
        "args": ["--verbose"],
        "environment": {"X": "1"},
        "environmentFile": None,
        "blockOnFailure": block_on_failure,
    }


def test_load_hooks_parses_all_seven_keys(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "preStart": [_hook_json("/bin/pre-start")],
                "onSuccess": [_hook_json("/bin/on-success")],
                "onFailure": [_hook_json("/bin/on-failure")],
                "postRun": [_hook_json("/bin/post-run")],
                "perDatabase": {
                    "preStart": [_hook_json("/bin/db-pre-start")],
                    "onSuccess": [_hook_json("/bin/db-on-success")],
                    "onFailure": [_hook_json("/bin/db-on-failure")],
                },
            }
        )
    )

    hooks = load_hooks(str(path))

    assert hooks.pre_start == [
        Hook(
            path="/bin/pre-start",
            args=["--verbose"],
            environment={"X": "1"},
            block_on_failure=True,
        )
    ]
    assert hooks.on_success[0].path == "/bin/on-success"
    assert hooks.on_failure[0].path == "/bin/on-failure"
    assert hooks.post_run[0].path == "/bin/post-run"
    assert hooks.per_database.pre_start[0].path == "/bin/db-pre-start"
    assert hooks.per_database.on_success[0].path == "/bin/db-on-success"
    assert hooks.per_database.on_failure[0].path == "/bin/db-on-failure"


def test_load_hooks_with_environment_file_and_missing_lists(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "preStart": [
                    {
                        "path": "/bin/true",
                        "args": [],
                        "environment": {},
                        "environmentFile": "/run/secrets/token",
                        "blockOnFailure": False,
                    }
                ]
            }
        )
    )

    hooks = load_hooks(str(path))

    assert hooks.pre_start[0].environment_file == "/run/secrets/token"
    assert hooks.pre_start[0].block_on_failure is False
    # keys absent from the JSON entirely (not just empty lists) default
    # to empty -- the Nix side always writes all seven, but the parser
    # itself shouldn't assume that.
    assert hooks.on_success == []
    assert hooks.per_database.on_failure == []
