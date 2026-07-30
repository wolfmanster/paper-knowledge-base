"""
pkb.py — Monorepo root wrapper for paper-knowledge-base search.

Usage:
    python pkb.py "your query" [top_k]
    python pkb.py --mode text "keywords" [top_k]
    python pkb.py --version

All arguments are forwarded as-is to paper-knowledge-base/scripts/query.py.
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
QUERY_SCRIPT = BASE / "paper-knowledge-base" / "scripts" / "query.py"
CWD = BASE / "paper-knowledge-base"


def main() -> int:
    if not QUERY_SCRIPT.exists():
        print(f"Error: {QUERY_SCRIPT} not found", file=sys.stderr)
        return 1

    return subprocess.call(
        [sys.executable, str(QUERY_SCRIPT)] + sys.argv[1:],
        cwd=str(CWD),
    )


if __name__ == "__main__":
    sys.exit(main())
