"""Fast tier: hook loading, environment merging, and invocation.

New in the hooks redesign -- see docs/decisions/0006 for why every hook
(including the systemd-triggered onFailure one) goes through this same
code, and why a collision between environment sources is a hard error,
never a silent override.
"""

from __future__ import annotations

import pytest

from collation_guard.hooks import (
    EnvironmentCollisionError,
    merge_environment,
    parse_environment_file,
)


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
