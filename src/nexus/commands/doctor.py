# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import chromadb
import click
import structlog

from nexus.config import get_credential
from nexus.db.t3 import _STORE_TYPES
from nexus.commands.hooks import _effective_hooks_dir, SENTINEL_BEGIN
from nexus.commands._helpers import default_db_path
from nexus.registry import RepoRegistry
from nexus.session import SESSIONS_DIR

_log = structlog.get_logger(__name__)

_CHECK = "✓"
_WARN  = "✗"


def _check_line(label: str, ok: bool, detail: str = "") -> str:
    status = _CHECK if ok else _WARN
    msg = f"  {status} {label}"
    if detail:
        msg += f": {detail}"
    return msg


def _fix(lines: list[str], *fix_lines: str) -> None:
    """Append indented Fix: lines after a failure entry."""
    first = True
    for fix_line in fix_lines:
        if first:
            lines.append(f"    Fix: {fix_line}")
            first = False
        else:
            lines.append(f"         {fix_line}")


def _python_ok() -> tuple[bool, str]:
    """Return (meets_requirement, version_string) for the running Python."""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    return vi >= (3, 12), ver


# Keep old name so existing tests importing `_check` still work.
def _check(label: str, ok: bool, detail: str = "") -> str:
    return _check_line(label, ok, detail)


def _check_orphan_t1(lines: list[str]) -> bool:
    """Check for orphaned T1 session files. Returns True if all clean."""
    if not SESSIONS_DIR.exists():
        lines.append(_check_line("T1 sessions", True, "no sessions directory"))
        return True

    session_files = list(SESSIONS_DIR.glob("*.session"))
    if not session_files:
        lines.append(_check_line("T1 sessions", True, "no session files"))
        return True

    orphans: list[str] = []
    for sf in session_files:
        try:
            record = json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            continue  # corrupt/unreadable — skip
        pid = record.get("server_pid")
        if pid is None:
            continue
        try:
            os.kill(int(pid), 0)  # raises OSError if process is dead
        except OSError:
            orphans.append(sf.name)

    if orphans:
        lines.append(_check_line(
            "T1 sessions", False,
            f"{len(orphans)} orphaned session file(s): {', '.join(orphans)}",
        ))
        _fix(lines,
             "Remove stale files: rm ~/.config/nexus/sessions/*.session",
             "Or run: nx doctor (sweep runs automatically on session start)")
        return False

    lines.append(_check_line(
        "T1 sessions", True,
        f"{len(session_files)} session file(s), all processes live",
    ))
    return True


def _check_t2_integrity(lines: list[str]) -> bool:
    """Verify T2 SQLite + FTS5 index integrity. Returns True if all ok."""
    db_path = default_db_path()
    if not db_path.exists():
        lines.append(_check_line("T2 integrity", True, "not created yet"))
        return True

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # PRAGMA integrity_check returns one row per problem; "ok" means clean.
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            pragma_ok = len(rows) == 1 and rows[0][0] == "ok"
            if not pragma_ok:
                issues = "; ".join(r[0] for r in rows[:3])
                lines.append(_check_line("T2 integrity", False, f"PRAGMA: {issues}"))
                return False

            # FTS5 integrity-check: raises OperationalError if index is corrupt.
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')")
            fts_ok = True
        except sqlite3.OperationalError as exc:
            lines.append(_check_line("T2 integrity", False, f"FTS5: {exc}"))
            return False
        finally:
            conn.close()
    except Exception as exc:
        lines.append(_check_line("T2 integrity", False, f"could not open: {exc}"))
        return False

    if pragma_ok and fts_ok:
        lines.append(_check_line("T2 integrity", True, "PRAGMA ok, FTS5 ok"))
    return pragma_ok and fts_ok


def _check_chroma_pagination(lines: list[str], client: object, db_name: str) -> bool:
    """Spot-check one non-empty collection's count() vs paginated get(). Returns True if ok."""
    try:
        cols = client.list_collections()  # type: ignore[union-attr]
    except Exception as exc:
        lines.append(_check_line(f"ChromaDB pagination ({db_name})", False, f"list failed: {exc}"))
        return False

    # Find the first non-empty collection to audit.
    target_col = None
    for col in cols:
        try:
            if col.count() > 0:
                target_col = col
                break
        except Exception:
            continue

    if target_col is None:
        lines.append(_check_line(f"ChromaDB pagination ({db_name})", True, "no non-empty collections to audit"))
        return True

    try:
        expected = target_col.count()
        retrieved = 0
        offset = 0
        page_size = 300
        while True:
            batch = target_col.get(limit=page_size, offset=offset)
            ids = batch.get("ids", [])
            retrieved += len(ids)
            if len(ids) < page_size:
                break
            offset += page_size

        ok = retrieved == expected
        detail = (
            f"{target_col.name}: count={expected}, paginated={retrieved}"
        )
        lines.append(_check_line(f"ChromaDB pagination ({db_name})", ok, detail))
        return ok
    except Exception as exc:
        lines.append(_check_line(f"ChromaDB pagination ({db_name})", False, f"audit failed: {exc}"))
        return False


@click.command("doctor")
def doctor_cmd() -> None:
    """Verify that all required services and credentials are available."""
    lines: list[str] = ["Nexus health check:\n"]
    failed = False

    # ── Python version ────────────────────────────────────────────────────────
    py_ok, py_ver = _python_ok()
    lines.append(_check_line(
        "Python ≥ 3.12",
        py_ok,
        py_ver if py_ok else f"{py_ver} — 3.12+ required",
    ))
    if not py_ok:
        failed = True
        _fix(lines,
             "https://www.python.org/downloads/",
             "brew install python@3.12         (macOS)",
             "apt install python3.12           (Ubuntu/Debian)")

    # ── CHROMA_API_KEY ────────────────────────────────────────────────────────
    chroma_key = get_credential("chroma_api_key")
    lines.append(_check_line("ChromaDB  (CHROMA_API_KEY)",  bool(chroma_key),
                              "set" if chroma_key else "not set"))
    if not chroma_key:
        failed = True
        _fix(lines,
             "nx config init                           (interactive wizard)",
             "nx config set chroma_api_key <your-key>  (set individually)",
             "Get key: https://trychroma.com  →  Cloud  →  API Keys")

    # ── CHROMA_TENANT (optional — inferred from API key if not set) ──────────
    chroma_tenant = get_credential("chroma_tenant")
    lines.append(_check_line("ChromaDB  (CHROMA_TENANT)",
                              True,
                              chroma_tenant if chroma_tenant
                              else "not set (auto-inferred from API key — set explicitly only for multi-workspace)"))

    # ── CHROMA_DATABASE ───────────────────────────────────────────────────────
    chroma_database = get_credential("chroma_database")
    lines.append(_check_line("ChromaDB  (CHROMA_DATABASE)", bool(chroma_database),
                              chroma_database if chroma_database else "not set"))
    if not chroma_database:
        failed = True
        _fix(lines,
             "nx config init                           (interactive wizard, also provisions databases)",
             "nx config set chroma_database <base-name>",
             "e.g. nx config set chroma_database nexus")

    # ── ChromaDB four-store databases ────────────────────────────────────────
    if chroma_key and chroma_database:
        db_ok = True
        for t in _STORE_TYPES:
            db_name = f"{chroma_database}_{t}"
            try:
                chromadb.CloudClient(
                    tenant=chroma_tenant or None, database=db_name, api_key=chroma_key
                )
                lines.append(_check_line(f"ChromaDB  ({db_name})", True, "reachable"))
            except Exception as exc:
                db_ok = False
                failed = True
                _log.debug("db_not_reachable", db_name=db_name, error=str(exc))
                lines.append(_check_line(f"ChromaDB  ({db_name})", False, "not reachable"))
        if not db_ok:
            _fix(lines,
                 "Run 'nx config init' to provision databases automatically — no dashboard visit needed.",
                 "Or create these databases manually in the ChromaDB Cloud dashboard:",
                 *[f"  - {chroma_database}_{t}" for t in _STORE_TYPES])

    # ── VOYAGE_API_KEY ────────────────────────────────────────────────────────
    voyage_key = get_credential("voyage_api_key")
    lines.append(_check_line("Voyage AI (VOYAGE_API_KEY)",  bool(voyage_key),
                              "set" if voyage_key else "not set"))
    if not voyage_key:
        failed = True
        _fix(lines,
             "nx config init                           (interactive wizard)",
             "nx config set voyage_api_key <your-key>  (set individually)",
             "Get key: https://voyageai.com  →  Dashboard  →  API Keys")

    # ── Pipeline version check ───────────────────────────────────────────────
    if chroma_key and chroma_database and voyage_key:
        from nexus.indexer import PIPELINE_VERSION, get_collection_pipeline_version

        stale_count = 0
        for store_type in _STORE_TYPES:
            db_name = f"{chroma_database}_{store_type}"
            try:
                client = chromadb.CloudClient(
                    tenant=chroma_tenant or None, database=db_name, api_key=chroma_key
                )
                cols = client.list_collections()
                for col in cols:
                    stored = get_collection_pipeline_version(col)
                    if stored is None:
                        lines.append(_check_line(
                            f"pipeline ({col.name})", True,
                            "no version stamp (index with --force to stamp)",
                        ))
                    elif stored != PIPELINE_VERSION:
                        stale_count += 1
                        lines.append(_check_line(
                            f"pipeline ({col.name})", False,
                            f"v{stored} (current: v{PIPELINE_VERSION})",
                        ))
                    else:
                        lines.append(_check_line(
                            f"pipeline ({col.name})", True, f"v{stored}",
                        ))
            except Exception as exc:
                _log.debug("doctor_pipeline_check_failed", db=db_name, error=str(exc))
                lines.append(_check_line(f"pipeline ({db_name})", False, "check failed"))
        if stale_count:
            failed = True
            _fix(lines,
                 "nx index repo <path> --force-stale  (re-index outdated collections)",
                 "nx index repo <path> --force        (re-index all collections)")

    # ── ripgrep ───────────────────────────────────────────────────────────────
    rg_path = shutil.which("rg")
    lines.append(_check_line("ripgrep   (rg)",              bool(rg_path),
                              rg_path or "not found — hybrid search disabled"))
    if not rg_path:
        failed = True
        _fix(lines,
             "brew install ripgrep                      (macOS)",
             "apt install ripgrep                       (Ubuntu/Debian)",
             "https://github.com/BurntSushi/ripgrep#installation")

    # ── git ───────────────────────────────────────────────────────────────────
    git_path = shutil.which("git")
    lines.append(_check_line("git",                         bool(git_path),
                              git_path or "not found on PATH"))
    if not git_path:
        failed = True
        _fix(lines,
             "brew install git                          (macOS)",
             "apt install git                           (Ubuntu/Debian)",
             "https://git-scm.com/downloads")

    # ── bd (beads, optional) ──────────────────────────────────────────────────
    bd_path = shutil.which("bd")
    if bd_path:
        lines.append(_check_line("bd (beads, optional)", True, bd_path))
    else:
        lines.append(_check_line("bd (beads, optional)", True,
                                 "not found — task tracking unavailable"))
        _fix(lines, "https://github.com/BeadsProject/beads")

    # ── git hooks ─────────────────────────────────────────────────────────────
    # Hooks are optional and always non-fatal — always ✓, report status in detail.
    _hook_names = ("post-commit", "post-merge", "post-rewrite")
    _registry_path = Path.home() / ".config" / "nexus" / "repos.json"

    try:
        reg = RepoRegistry(_registry_path)
        repos = reg.all()
    except Exception as exc:
        _log.warning("doctor_registry_load_failed", error=str(exc))
        repos = []

    if not repos:
        lines.append(_check_line("git hooks", True,
                                 "no repos registered — run: nx index repo <path>"))
    else:
        for repo_str in repos:
            repo_path = Path(repo_str)
            try:
                hdir = _effective_hooks_dir(repo_path)
                installed = [
                    n for n in _hook_names
                    if (hdir / n).exists() and SENTINEL_BEGIN in (hdir / n).read_text()
                ]
                if installed:
                    lines.append(_check_line("git hooks", True,
                                             f"{repo_path} ({', '.join(installed)})"))
                else:
                    lines.append(_check_line("git hooks", True,
                                             f"{repo_path} — not installed"))
                    _fix(lines, f"nx hooks install {repo_path}")
            except Exception:
                lines.append(_check_line("git hooks", True,
                                         f"{repo_path} — could not check"))

    # ── index log ─────────────────────────────────────────────────────────────
    import time as _time
    log_path = Path.home() / ".config" / "nexus" / "index.log"
    if log_path.exists():
        mtime = log_path.stat().st_mtime
        age_s = _time.time() - mtime
        if age_s < 60:
            age_str = f"{int(age_s)}s ago"
        elif age_s < 3600:
            age_str = f"{int(age_s // 60)} minutes ago"
        else:
            age_str = f"{int(age_s // 3600)} hours ago"
        lines.append(_check_line("index log", True,
                                 f"{log_path} (last write: {age_str})"))
    else:
        lines.append(_check_line("index log", True,
                                 f"{log_path} (not created yet — git hooks have not fired)"))

    # ── Orphan T1 process detection ───────────────────────────────────────────
    # Non-fatal: stale session files are annoying but do not block operation.
    _check_orphan_t1(lines)

    # ── T2 database integrity ─────────────────────────────────────────────────
    # Non-fatal: integrity failure is logged but does not set failed=True.
    _check_t2_integrity(lines)

    # ── ChromaDB pagination audit ─────────────────────────────────────────────
    # Spot-check one non-empty collection per configured store (non-fatal).
    if chroma_key and chroma_database:
        for t in _STORE_TYPES:
            db_name = f"{chroma_database}_{t}"
            try:
                client = chromadb.CloudClient(
                    tenant=chroma_tenant or None, database=db_name, api_key=chroma_key
                )
                _check_chroma_pagination(lines, client, db_name)
            except Exception as exc:
                _log.debug("doctor_pagination_check_client_failed", db=db_name, error=str(exc))
                lines.append(_check_line(f"ChromaDB pagination ({db_name})", True,
                                         "skipped (client unavailable)"))

    click.echo("\n".join(lines))

    if failed:
        click.echo("\nRun 'nx config init' to set up credentials and provision databases automatically.")
        raise click.exceptions.Exit(1)
