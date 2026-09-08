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
The writer half is DELETED rather than switched on: a never-watched
two-way writer whose first live run would have resolved nine known file/T2
disagreements by a ranking rule nobody had seen work was the risky thing
here, not the missing feature. (The ruling's other ground -- that ``nx rdr
set-status`` now writes file and T2 together -- is not so: set-status
writes the file and README, the lifecycle skills write T2. The drift class
therefore still exists and is DETECTED, not reconciled: ``nx rdr preamble
rdr-audit`` prints a ``DRIFT:`` line per disagreement for a human to
settle.) The read-only summary is kept and the filter fixed so it finally
prints. The nine known drift rows are bead nexus-nxn5g.
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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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
    before ``nx index repo`` has run. Returns ``None`` when no
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
    except Exception as exc:  # noqa: BLE001 — the SessionStart hook must never fail; the reason is logged, not swallowed (nexus-owna8)
        _log_resolution_error("catalog", exc)
    try:
        from nexus.indexer import _repo_collection_or_legacy  # noqa: PLC0415

        return _repo_collection_or_legacy(repo_root, "rdr")
    except Exception as exc:  # noqa: BLE001 — same contract as above
        _log_resolution_error("path-derived", exc)
        return None


def _log_resolution_error(source: str, exc: BaseException) -> None:
    """nexus-owna8: a blind except here forced every session onto the
    path-derived fallback, whose owner id can differ from the catalog's, and
    the hook then reported a fully indexed tree as NOT indexed. The failure
    is logged so the next false verdict names its cause."""
    # stderr FIRST, with nothing but sys: on 2026-09-08 the interpreter that
    # ran this hook had neither nexus nor structlog, so the structlog line
    # below could not be written and the false NOT-indexed verdict shipped
    # with an empty stderr (nexus-4ti7e). A guard that needs the package it
    # guards is no guard.
    try:
        sys.stderr.write(
            f"rdr_hook: collection resolution failed ({source}): "
            f"{type(exc).__name__}: {exc} [python {sys.executable}]\n"
        )
    except Exception:  # noqa: BLE001 — even stderr is best-effort in a hook
        pass
    try:
        import structlog  # noqa: PLC0415

        structlog.get_logger(__name__).warning(
            "rdr_hook_collection_resolution_failed",
            source=source, error_type=type(exc).__name__, error=str(exc),
        )
    except Exception:  # noqa: BLE001 — even the log is best-effort in a hook
        pass


# hooks.json caps this hook at 10s and the T3 client's own request timeout
# is 30s, so a slow-but-reachable store would have the harness kill the hook
# before the listing fallback ever ran (review of a71c93e92). Both halves
# get a budget that fits inside the cap.
_T3_DEADLINE_S = 4.0
_LISTING_TIMEOUT_S = 4


def _collection_exists(target: str) -> bool:
    """Whether *target* exists in T3, asked of the store itself
    (nexus-owna8: the previous substring match over ``nx collection list``
    output missed a listed collection when the resolved name and the
    listed name were rendered differently). The T3 call runs under
    ``_T3_DEADLINE_S``; past it, or on any error, the listing is the fallback."""
    try:
        from nexus.db import make_t3  # noqa: PLC0415

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(lambda: bool(make_t3().collection_exists(target)))
            return future.result(timeout=_T3_DEADLINE_S)
        finally:
            pool.shutdown(wait=False)
    except FutureTimeout:
        _log_resolution_error("t3-exists", TimeoutError(f"no answer within {_T3_DEADLINE_S}s"))
    except Exception as exc:  # noqa: BLE001 — the hook must never fail; fall back to the listing
        _log_resolution_error("t3-exists", exc)
    try:
        result = subprocess.run(
            ["nx", "collection", "list"],
            capture_output=True, text=True, timeout=_LISTING_TIMEOUT_S,
        )
        if result.returncode == 0:
            return target in result.stdout
    except Exception:  # noqa: BLE001 — best-effort fallback
        pass
    return False


def _extract_rdr_id(filepath: Path) -> str | None:
    """Numeric RDR id from a ``docs/rdr`` filename, or ``None`` for a file
    that is not an RDR document (see :data:`_RDR_STEM_RE`). This is the
    file filter ``main`` applies; nexus-e19sa's whole lesson is that a
    filter selecting nothing looks exactly like a quiet success."""
    m = _RDR_STEM_RE.match(filepath.stem)
    return m.group(1) if m else None


_T2_ROWS_CACHE: dict[str, list[dict]] = {}


def _fetch_rdr_rows(repo_name: str) -> list[dict]:
    """One ``get_all`` per hook run, shared by the status and gate loaders
    (code review [24883] finding 5: two full fetches of a 1000-row project
    inside a 10s SessionStart budget)."""
    if repo_name in _T2_ROWS_CACHE:
        return _T2_ROWS_CACHE[repo_name]
    rows: list[dict] = []
    try:
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database

        with T2Database(default_db_path()) as db:
            rows = list(db.get_all(project=f"{repo_name}_rdr"))
    except Exception:
        rows = []
    _T2_ROWS_CACHE[repo_name] = rows
    return rows


def _load_all_t2_statuses(repo_name: str) -> dict[str, str]:
    """Batch-load all T2 RDR statuses. Returns {rdr_id: status}."""
    statuses: dict[str, str] = {}
    try:
        if True:
            entries = _fetch_rdr_rows(repo_name)
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


def _load_gated_commits(repo_name: str) -> dict[str, str]:
    """``{rdr_id: commit}`` from every ``<id>-gate-latest`` T2 record that
    carries a ``commit:`` field (nexus-zbdm0)."""
    gated: dict[str, str] = {}
    try:
        for entry in _fetch_rdr_rows(repo_name):
            title = entry.get("title", "")
            if not title.endswith("-gate-latest"):
                continue
            # Keyed on the bare number ("RDR-105-gate-latest" and
            # "097-gate-latest" both normalise), matching _extract_rdr_id.
            num = re.search(r"(\d+)", title[: -len("-gate-latest")])
            if not num:
                continue
            for line in entry.get("content", "").splitlines():
                stripped = line.strip()
                if stripped.startswith("commit:"):
                    val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        gated[str(int(num.group(1)))] = val
                    break
    except Exception:
        pass
    return gated


def _unchecked_fix_edits(root: Path, rdr_files: list[Path], statuses: dict[str, str], gated: dict[str, str]) -> list[str]:
    """Lines naming draft RDRs whose file tip is past the gated commit."""
    lines: list[str] = []
    for path in rdr_files:
        rid = _extract_rdr_id(path)
        if rid is None:
            continue
        key = str(int(rid))
        gated_norm = {str(int(k)): v for k, v in gated.items() if k.isdigit()}
        if key not in gated_norm:
            continue
        if statuses.get(rid, statuses.get(key, "draft")) not in ("draft", "open"):
            continue
        try:
            tip = subprocess.run(
                ["git", "-C", str(root), "log", "-1", "--format=%h", "--", str(path)],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if not tip:
            continue  # untracked or uncommitted file: unknown, not "past the gate"
        commit = gated_norm[key]
        n = min(len(tip), len(commit))
        if n >= 7 and tip[:n] == commit[:n]:
            continue
        lines.append(
            f"RDR-{rid}: edits since the gated commit {commit}; run /conexus:rdr-fix {rid} "
            "before re-gating (fix check first)."
        )
    return lines


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
    ``docs/rdr/post-mortem/`` (a separate document set) never carries an
    RDR status, with the index/template/agents files excluded by name."""
    return [
        p for p in rdr_dir.glob("*.md")
        if p.name.lower() not in _EXCLUDE_FILES and _extract_rdr_id(p) is not None
    ]


def _indexed_document_count(rdr_dir: Path) -> int:
    """Every markdown file the repo indexer registers under *rdr_dir*: the
    same recursive walk as ``nx index repo`` (joint/ and post-mortem/
    included, README and AGENTS included). nexus-owna8: the hook reported
    the RDR count against a collection holding this count, and the two
    numbers (215 vs 298) read as a partial index."""
    return sum(1 for p in rdr_dir.rglob("*.md") if p.is_file() and not p.is_symlink())


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

    statuses = _load_all_t2_statuses(repo_name)
    counts = _rdr_status_counts(repo_name, statuses)
    documents = _indexed_document_count(rdr_dir)
    if counts:
        breakdown = ", ".join(f"{n} {s}" for s, n in counts.most_common())
        status_info = f"{documents} documents ({len(rdr_files)} RDRs: {breakdown})"
    else:
        status_info = f"{documents} documents ({len(rdr_files)} RDRs)"

    if indexed:
        print(f"RDR: {status_info}, indexed in {rdr_collection}")
    else:
        # nexus-3o4lt: the remedy is the REPO indexer. This line used to
        # say ``nx index rdr <root>``, which registered every RDR under
        # the curator owner with an absolute path; on the work box that
        # produced 198 such rows that no owner-scoped reader could see.
        # ``nx index repo`` walks docs/rdr under the repo owner. The
        # single-file ``nx index rdr <file>`` now lands there too, but the
        # whole-tree remedy is the repo index.
        print(f"RDR: {status_info} in {rdr_dir.relative_to(root)} but NOT indexed.")
        if rdr_collection:
            print(f"     Run: nx index repo {root}")
        else:
            print(f"     Run: nx index repo {root}")

    for line in _unchecked_fix_edits(root, rdr_files, statuses, _load_gated_commits(repo_name)):
        print(line)

    sys.exit(0)


if __name__ == "__main__":
    main()
