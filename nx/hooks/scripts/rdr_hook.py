#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: detect RDR dir, reconcile file↔T2 status, report."""
from __future__ import annotations

import sys
if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: nx plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

import re
import subprocess
from collections import Counter
from pathlib import Path


# Monotonic status ordering (higher index = more advanced)
_STATUS_ORDER = {
    "draft": 0,
    "accepted": 1,
    "implemented": 2,
    "closed": 3,
    "reverted": 4,
    "abandoned": 4,
    "superseded": 4,
}
_TERMINAL = {"closed", "reverted", "abandoned", "superseded"}
_EXCLUDE_FILES = {
    "readme.md", "template.md", "index.md", "overview.md",
    "workflow.md", "templates.md",
}


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


def _parse_frontmatter_status(filepath: Path) -> str | None:
    """Extract status from YAML frontmatter."""
    try:
        text = filepath.read_text(errors="replace")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    block = parts[1]
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("status:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return val.lower() if val else None
    return None


def _extract_rdr_id(filepath: Path) -> str | None:
    """Extract numeric ID from filename like 001-foo.md."""
    m = re.match(r"(\d+)", filepath.stem)
    return m.group(1) if m else None


def _load_all_t2_statuses(repo_name: str) -> dict[str, str]:
    """Batch-load all T2 RDR statuses. Returns {rdr_id: status}."""
    statuses: dict[str, str] = {}
    try:
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database

        with T2Database(default_db_path()) as db:
            entries = db.get_all(project=f"{repo_name}_rdr")
            for entry in entries:
                title = entry.get("title", "")
                if "-" in title:
                    continue  # skip gate-latest, research, etc.
                content = entry.get("content", "")
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("status:"):
                        val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                        if val:
                            statuses[title] = val.lower()
                        break
    except Exception:
        pass
    return statuses


def _update_t2_status(repo_name: str, rdr_id: str, new_status: str) -> bool:
    """Update T2 record status field. Reads existing, replaces status line."""
    try:
        result = subprocess.run(
            ["nx", "memory", "get", "--project", f"{repo_name}_rdr", "--title", rdr_id],
            capture_output=True, text=True, timeout=10,
        )
        content = (result.stdout or "").strip()
        if not content:
            return False
        # Replace status line
        updated_lines = []
        for line in content.splitlines():
            if line.strip().startswith("status:"):
                updated_lines.append(f'status: "{new_status}"')
            else:
                updated_lines.append(line)
        updated = "\n".join(updated_lines)
        result = subprocess.run(
            ["nx", "memory", "put", "-", "--project", f"{repo_name}_rdr",
             "--title", rdr_id, "--ttl", "permanent", "--tags", "rdr"],
            input=updated, capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _update_file_status(filepath: Path, new_status: str) -> bool:
    """Update frontmatter status in RDR file."""
    try:
        text = filepath.read_text(errors="replace")
        if not text.startswith("---"):
            return False
        parts = text.split("---", 2)
        if len(parts) < 3:
            return False
        # Replace status line in frontmatter
        fm_lines = parts[1].splitlines()
        new_fm = []
        for line in fm_lines:
            if line.strip().startswith("status:"):
                new_fm.append(f"status: {new_status}")
            else:
                new_fm.append(line)
        new_text = "---\n" + "\n".join(new_fm).strip() + "\n---" + parts[2]
        filepath.write_text(new_text)
        return True
    except Exception:
        return False


def _reconcile(root: Path, repo_name: str, rdr_files: list[Path],
               t2_statuses: dict[str, str]) -> int:
    """Reconcile file↔T2 status. Returns count of reconciled records."""
    reconciled = 0
    for filepath in rdr_files:
        rdr_id = _extract_rdr_id(filepath)
        if not rdr_id:
            continue

        file_status = _parse_frontmatter_status(filepath)
        t2_status = t2_statuses.get(rdr_id)

        if file_status is None or t2_status is None:
            continue
        if file_status == t2_status:
            continue

        file_rank = _STATUS_ORDER.get(file_status, -1)
        t2_rank = _STATUS_ORDER.get(t2_status, -1)

        # Both terminal but different — warn, do NOT auto-reconcile
        if file_status in _TERMINAL and t2_status in _TERMINAL and file_status != t2_status:
            print(f"     WARNING: RDR {rdr_id} terminal conflict "
                  f"(T2={t2_status}, file={file_status}) — manual resolution needed")
            continue
        elif file_rank > t2_rank:
            # File is more advanced — update T2
            print(f"     RDR {rdr_id}: {t2_status} → {file_status} (syncing T2 to file)")
            _update_t2_status(repo_name, rdr_id, file_status)
            reconciled += 1
        elif t2_rank > file_rank:
            # T2 is more advanced — update file
            print(f"     RDR {rdr_id}: {file_status} → {t2_status} (syncing file to T2)")
            _update_file_status(filepath, t2_status)
            reconciled += 1

    return reconciled


def _rdr_status_counts(repo_name: str, preloaded: dict[str, str] | None = None) -> Counter[str]:
    """Status counts from T2. Uses preloaded statuses if available."""
    statuses = preloaded if preloaded is not None else _load_all_t2_statuses(repo_name)
    return Counter(statuses.values())


def _rdr_dir(root: Path) -> Path:
    """Resolve RDR directory from .nexus.yml or fall back to docs/rdr."""
    config_path = root / ".nexus.yml"
    if config_path.exists():
        try:
            import yaml
            with config_path.open() as fh:
                data = yaml.safe_load(fh) or {}
            paths = data.get("indexing", {}).get("rdr_paths", [])
            if paths:
                return root / paths[0]
        except Exception:
            pass
    return root / "docs" / "rdr"


def main() -> None:
    root = _repo_root()
    if root is None:
        sys.exit(0)

    rdr_dir = _rdr_dir(root)
    if not rdr_dir.exists():
        sys.exit(0)

    rdr_files = [
        p for p in rdr_dir.glob("*.md")
        if p.name.lower() not in _EXCLUDE_FILES
        and re.match(r"\d+", p.stem)
    ]
    if not rdr_files:
        sys.exit(0)

    repo_name = root.name
    indexed = _collection_exists(repo_name)

    # Batch-load T2 statuses once (used by both reconcile and status counts)
    t2_statuses = _load_all_t2_statuses(repo_name)

    # Reconcile file↔T2 status (monotonic-advance rule)
    reconciled = _reconcile(root, repo_name, rdr_files, t2_statuses)
    if reconciled:
        print(f"RDR sync: {reconciled} record(s) reconciled")
        # Reload after reconciliation changed statuses
        t2_statuses = _load_all_t2_statuses(repo_name)

    # Status breakdown from T2 (post-reconciliation)
    counts = _rdr_status_counts(repo_name, preloaded=t2_statuses)
    if counts:
        breakdown = ", ".join(f"{n} {s}" for s, n in counts.most_common())
        status_info = f"{len(rdr_files)} documents ({breakdown})"
    else:
        status_info = f"{len(rdr_files)} document(s)"

    if indexed:
        print(f"RDR: {status_info}, indexed in rdr__{repo_name}")
    else:
        print(f"RDR: {status_info} in {rdr_dir.relative_to(root)} but NOT indexed.")
        print(f"     Run: nx index rdr {root}")

    sys.exit(0)


if __name__ == "__main__":
    main()
