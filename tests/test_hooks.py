"""Fast tier: hook loading, environment merging, and invocation.

New in the hooks redesign -- see docs/decisions/0006 for why every hook
(including the systemd-triggered onFailure one) goes through this same
code, and why a collision between environment sources is a hard error,
never a silent override.
"""

from __future__ import annotations

from collation_guard.hooks import parse_environment_file


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
