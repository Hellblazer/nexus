#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: detect docs/rdr/ and report RDR indexing status."""
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _collection_exists(repo_name: str) -> bool:
    try:
        result = subprocess.run(
            ["nx", "collection", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            target = f"docs__rdr__{repo_name}"
            return target in result.stdout
    except Exception:
        pass
    return False


def main() -> None:
    root = _repo_root()
    if root is None:
        sys.exit(0)

    rdr_dir = root / "docs" / "rdr"
    if not rdr_dir.exists():
        sys.exit(0)

    exclude = {"README.md", "TEMPLATE.md"}
    rdr_files = [p for p in rdr_dir.glob("*.md") if p.name not in exclude]
    if not rdr_files:
        sys.exit(0)

    repo_name = root.name
    indexed = _collection_exists(repo_name)

    if indexed:
        print(f"RDR: {len(rdr_files)} document(s) in docs/rdr/, indexed in docs__rdr__{repo_name}")
    else:
        print(f"RDR: {len(rdr_files)} document(s) found in docs/rdr/ but NOT indexed.")
        print(f"     Run: nx index rdr {root}")

    sys.exit(0)


if __name__ == "__main__":
    main()
