#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: detect docs/rdr/ and report RDR status and indexing."""
import subprocess
import sys
from collections import Counter
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
            target = f"rdr__{repo_name}"
            return target in result.stdout
    except Exception:
        pass
    return False


def _rdr_status_counts(repo_name: str) -> Counter[str]:
    """Query T2 for RDR status counts. Returns empty Counter on failure."""
    counts: Counter[str] = Counter()
    try:
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database

        with T2Database(default_db_path()) as db:
            entries = db.get_all(project=f"{repo_name}_rdr")
            for entry in entries:
                for line in entry["content"].splitlines():
                    stripped = line.strip()
                    if stripped.startswith("status:"):
                        status = stripped.split('"')[1] if '"' in stripped else stripped.split(":")[1].strip()
                        counts[status] += 1
                        break
    except Exception:
        pass
    return counts


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

    # Try to get status breakdown from T2
    counts = _rdr_status_counts(repo_name)
    if counts:
        breakdown = ", ".join(f"{n} {s}" for s, n in counts.most_common())
        status_info = f"{len(rdr_files)} documents ({breakdown})"
    else:
        status_info = f"{len(rdr_files)} document(s)"

    if indexed:
        print(f"RDR: {status_info}, indexed in rdr__{repo_name}")
    else:
        print(f"RDR: {status_info} in docs/rdr/ but NOT indexed.")
        print(f"     Run: nx index rdr {root}")

    sys.exit(0)


if __name__ == "__main__":
    main()
