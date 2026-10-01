"""Entry point. Full orchestration (all databases, template0, the
C.UTF-8 stamp, partition repair) lands cycle by cycle -- see the
project's TDD implementation sequence in docs/decisions. Placeholder
for now so `collation-guard` is a real, importable console script from
the first commit that has source at all.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("collation-guard: not yet implemented past the Postgres-tracked path", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
