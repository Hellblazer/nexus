#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart hook: detect the RDR dir and report document count, T2 status
breakdown, and whether the tree is indexed. Read-only.

nexus-e19sa (Sam's ruling, 2026-09-02): this hook used to carry a second
half -- a file<->T2 status RECONCILE that rewrote whichever side ranked
lower (``_reconcile`` / ``_update_file_status`` / ``_update_t2_status`` and
the terminal-rank derivation feeding them). It never ran once: the file
filter was ``re.match(r"\\d+", p.stem)`` against stems shaped
``rdr-201-...``, so it matched zero files and the hook exited before any
logic, on every session since it was written. That killed both halves.
The writer half is DELETED rather than switched on: ``nx rdr set-status``
now writes the file and T2 together through the checked lifecycle table
(RDR-201 Phase 1), so the drift class the reconcile existed for is
designed out at the source, and a never-watched two-way writer whose first
live run would have resolved nine known file/T2 disagreements by a ranking
rule nobody had seen work was the risky thing here, not the missing
feature. The read-only summary is kept and the filter fixed so it finally
prints. The nine drift rows are a separate, hand-fixed follow-up.
"""
from __future__ import annotations

import sys
if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

import re
import subprocess
from collections import Counter
from pathlib import Path

_EXCLUDE_FILES = {
    "readme.md", "template.md", "index.md", "overview.md",
    "workflow.md", "templates.md", "agents.md",
}

#: The stems this repo's RDR files actually have: ``rdr-201-foo`` (the
#: standard shape), ``rdr137-foo`` (one legacy file with no second hyphen),
#: and the bare ``001-foo`` shape the original filter was written for and
#: nothing here ever used. Anchored at the start of the stem, so a sibling
#: like ``status-census-2026-09-01`` (digits, not leading) is not an RDR.
_RDR_STEM_RE = re.compile(r"(?:rdr-?)?(\d+)", re.IGNORECASE)


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


def _resolve_rdr_collection(repo_root: Path) -> str | None:
    """Resolve the indexed RDR collection name for ``repo_root``.

    Returns the conformant ``rdr__<owner>__voyage-context-3__v1`` name
    when both the catalog and an owner row exist; otherwise asks the
    indexer's :func:`_repo_collection_or_legacy` for the
    path-derived conformant fallback so SessionStart keeps working
    before ``nx catalog setup`` lands. Returns ``None`` when no
    in-process resolution is available; the caller treats that as
    "not indexed" rather than splicing a non-conformant 2-segment shape
    that the post-Phase-5 strict-naming guard would later reject.
    """
    try:
        # RDR-158 P4 (nrxs9 final review Critical-1): this branch imported
        # the deleted local ``Catalog``, so the broad except silently forced
        # EVERY session onto the path-derived fallback. The service catalog
        # carries the same lookup.
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415

        cat = make_catalog_reader()
        try:
            return cat.collection_for_repo(repo_root, "rdr").render()
        except LookupError:
            pass  # owner not registered yet, fall through
    except Exception:
        pass
    try:
        from nexus.indexer import _repo_collection_or_legacy  # noqa: PLC0415

        return _repo_collection_or_legacy(repo_root, "rdr")
    except Exception:
        return None


def _collection_exists(target: str) -> bool:
    try:
        result = subprocess.run(
            ["nx", "collection", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return target in result.stdout
    except Exception:
        pass
    return False


def _extract_rdr_id(filepath: Path) -> str | None:
    """Numeric RDR id from a ``docs/rdr`` filename, or ``None`` for a file
    that is not an RDR document (see :data:`_RDR_STEM_RE`). This is the
    file filter ``main`` applies; nexus-e19sa's whole lesson is that a
    filter selecting nothing looks exactly like a quiet success."""
    m = _RDR_STEM_RE.match(filepath.stem)
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


def _rdr_files(rdr_dir: Path) -> list[Path]:
    """The RDR documents directly under *rdr_dir* -- non-recursive, so
    ``docs/rdr/post-mortem/`` (a separate document set) is never counted,
    with the index/template/agents files excluded by name."""
    return [
        p for p in rdr_dir.glob("*.md")
        if p.name.lower() not in _EXCLUDE_FILES and _extract_rdr_id(p) is not None
    ]


def main() -> None:
    root = _repo_root()
    if root is None:
        sys.exit(0)

    rdr_dir = _rdr_dir(root)
    if not rdr_dir.exists():
        sys.exit(0)

    rdr_files = _rdr_files(rdr_dir)
    if not rdr_files:
        sys.exit(0)

    repo_name = root.name
    rdr_collection = _resolve_rdr_collection(root)
    indexed = bool(rdr_collection) and _collection_exists(rdr_collection)

    counts = _rdr_status_counts(repo_name)
    if counts:
        breakdown = ", ".join(f"{n} {s}" for s, n in counts.most_common())
        status_info = f"{len(rdr_files)} documents ({breakdown})"
    else:
        status_info = f"{len(rdr_files)} document(s)"

    if indexed:
        print(f"RDR: {status_info}, indexed in {rdr_collection}")
    else:
        print(f"RDR: {status_info} in {rdr_dir.relative_to(root)} but NOT indexed.")
        if rdr_collection:
            print(f"     Run: nx index rdr {root}")
        else:
            print(f"     Run: nx catalog setup && nx index rdr {root}")

    sys.exit(0)


if __name__ == "__main__":
    main()
