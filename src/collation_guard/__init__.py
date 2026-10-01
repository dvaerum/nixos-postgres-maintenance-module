"""Postgres collation-drift guard: reindex/refresh a cluster whose
collation library version changed, and repair text-partition bounds
that drifted as a result, before postgresql.target comes up.
"""

__version__ = "0.1.0"
