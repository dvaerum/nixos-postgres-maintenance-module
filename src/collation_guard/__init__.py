"""Postgres collation-drift guard: reindex/refresh a cluster whose
collation library version changed, and repair text-partition bounds
that drifted as a result, before postgresql.target comes up.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Derived from the installed package's metadata, which hatchling
    # writes from pyproject.toml's [project] version at build time --
    # not a second literal to keep in sync by hand.
    __version__ = version("collation-guard")
except PackageNotFoundError:
    # Running from a source checkout that was never `pip install -e .`'d
    # (e.g. tests, which import straight off pythonpath).
    __version__ = "0+unknown"
