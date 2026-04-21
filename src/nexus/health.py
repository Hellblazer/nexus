# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health check data model and runner for nx doctor / nx console."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import chromadb
import structlog

from nexus.commands._helpers import default_db_path

_log = structlog.get_logger(__name__)

_CHECK = "✓"
_WARN = "✗"


@dataclass
class HealthResult:
    """One health check result."""

    label: str
    ok: bool
    detail: str = ""
    fix_suggestions: list[str] = field(default_factory=list)
    fatal: bool = False


# ── Formatting ────────────────────────────────────────────────────────────────


def format_health_for_cli(
    results: list[HealthResult], *, local_mode: bool
) -> tuple[str, bool]:
    """Format health results for CLI output.

    Returns (formatted_output, any_fatal_failure).
    Output is byte-for-byte compatible with the prior inline doctor_cmd format.
    """
    lines: list[str] = ["Nexus health check:\n"]
    failed = False

    for r in results:
        status = _CHECK if r.ok else _WARN
        msg = f"  {status} {r.label}"
        if r.detail:
            msg += f": {r.detail}"
        lines.append(msg)

        if r.fix_suggestions:
            prefix = "Fix: " if not r.ok else "Suggest: "
            cont_indent = " " * (4 + len(prefix))
            for i, fix_line in enumerate(r.fix_suggestions):
                if i == 0:
                    lines.append(f"    {prefix}{fix_line}")
                else:
                    lines.append(f"{cont_indent}{fix_line}")

        if r.fatal and not r.ok:
            failed = True

    if failed:
        if local_mode:
            lines.append(
                "\nSome checks failed. Run 'nx doctor' again after fixing the issues above."
            )
        else:
            lines.append(
                "\nRun 'nx config init' to set up credentials and provision the database automatically."
            )

    return "\n".join(lines), failed


# ── Individual checks ────────────────────────────────────────────────────────


def _python_ok() -> tuple[bool, str]:
    """Return (meets_requirement, version_string) for the running Python."""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    return vi >= (3, 12), ver


def _check_python() -> list[HealthResult]:
    ok, ver = _python_ok()
    r = HealthResult(
        label="Python ≥ 3.12",
        ok=ok,
        detail=ver if ok else f"{ver} — 3.12+ required",
        fatal=True,
    )
    if not ok:
        r.fix_suggestions = [
            "https://www.python.org/downloads/",
            "brew install python@3.12         (macOS)",
            "apt install python3.12           (Ubuntu/Debian)",
        ]
    return [r]


def _check_cli_version() -> list[HealthResult]:
    """Check whether a newer conexus version is available on PyPI."""
    try:
        from importlib.metadata import version as _pkg_version

        current = _pkg_version("conexus")
    except Exception:
        return []  # silent — installed version unknown

    # Check PyPI for latest (3-second timeout, network-tolerant)
    import json
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/conexus/json",
            headers={"User-Agent": f"nx-doctor/{current}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = data["info"]["version"]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, TimeoutError):
        return [HealthResult(
            label="conexus version",
            ok=True,
            detail=f"{current} (PyPI check skipped — offline?)",
        )]

    # Compare via tuple parsing
    def _parse(v: str) -> tuple[int, ...]:
        try:
            parts = tuple(int(x) for x in v.split(".")[:3])
            return parts + (0,) * (3 - len(parts))
        except ValueError:
            return (0, 0, 0)

    cur_t = _parse(current)
    latest_t = _parse(latest)

    if cur_t >= latest_t:
        return [HealthResult(
            label="conexus version",
            ok=True,
            detail=f"{current} (latest)",
        )]

    r = HealthResult(
        label="conexus version",
        ok=True,  # not fatal — just informational
        detail=f"{current} → {latest} available",
    )
    r.fix_suggestions = [
        f"uv tool upgrade conexus    # → {latest}",
    ]
    return [r]


def _check_t3_local() -> list[HealthResult]:
    from nexus.config import _default_local_path

    results: list[HealthResult] = []
    results.append(HealthResult(label="T3 mode", ok=True, detail="local (no API keys needed)"))

    local_path = _default_local_path()
    path_exists = local_path.exists()
    if path_exists:
        try:
            test_file = local_path / ".doctor_test"
            test_file.touch()
            test_file.unlink()
            results.append(HealthResult(label="Local ChromaDB path", ok=True, detail=str(local_path)))
        except OSError:
            results.append(HealthResult(
                label="Local ChromaDB path",
                ok=False,
                detail=f"{local_path} — not writable",
                fix_suggestions=[f"Check permissions on {local_path}"],
                fatal=True,
            ))
    else:
        results.append(HealthResult(
            label="Local ChromaDB path",
            ok=True,
            detail=f"{local_path} (will be created on first index)",
        ))

    # Embedding model
    from nexus.db.local_ef import LocalEmbeddingFunction
    ef = LocalEmbeddingFunction()
    r = HealthResult(label="Embedding model", ok=True, detail=f"{ef.model_name} ({ef.dimensions}d)")
    if ef.model_name == "all-MiniLM-L6-v2":
        r.fix_suggestions = ["Upgrade: pip install conexus[local]  (768d bge-base, better quality)"]
    results.append(r)

    # Collection count and disk usage. Empty collections are kept on purpose
    # (they preserve embedding-model metadata so the next store_put doesn't
    # need to re-derive it) — surface the count so users aren't surprised by
    # a 0-chunk collection lingering after `nx store delete` of every entry.
    if path_exists:
        try:
            client = chromadb.PersistentClient(path=str(local_path))
            cols = client.list_collections()
            col_count = len(cols)
            empty_count = sum(1 for c in cols if c.count() == 0)
            total_bytes = sum(f.stat().st_size for f in local_path.rglob("*") if f.is_file())
            if total_bytes < 1024 * 1024:
                size_str = f"{total_bytes / 1024:.1f} KB"
            else:
                size_str = f"{total_bytes / (1024 * 1024):.1f} MB"
            empty_note = f" (including {empty_count} empty)" if empty_count else ""
            results.append(HealthResult(
                label="Local collections", ok=True,
                detail=f"{col_count} collections{empty_note}, {size_str} on disk",
            ))
        except Exception as exc:
            _log.debug("doctor_local_collections_failed", error=str(exc))
            results.append(HealthResult(label="Local collections", ok=True, detail="could not query"))

    return results


def _check_t3_cloud() -> list[HealthResult]:
    from nexus.config import get_credential

    results: list[HealthResult] = []
    results.append(HealthResult(label="T3 mode", ok=True, detail="cloud"))

    # CHROMA_API_KEY
    chroma_key = get_credential("chroma_api_key")
    r = HealthResult(
        label="ChromaDB  (CHROMA_API_KEY)",
        ok=bool(chroma_key),
        detail="set" if chroma_key else "not set",
        fatal=True,
    )
    if not chroma_key:
        r.fix_suggestions = [
            "nx config init                           (interactive wizard)",
            "nx config set chroma_api_key <your-key>  (set individually)",
            "Get key: https://trychroma.com  →  Cloud  →  API Keys",
        ]
    results.append(r)

    # CHROMA_TENANT (optional)
    chroma_tenant = get_credential("chroma_tenant")
    results.append(HealthResult(
        label="ChromaDB  (CHROMA_TENANT)",
        ok=True,
        detail=chroma_tenant if chroma_tenant
        else "not set (auto-inferred from API key — set explicitly only for multi-workspace)",
    ))

    # CHROMA_DATABASE
    chroma_database = get_credential("chroma_database")
    r = HealthResult(
        label="ChromaDB  (CHROMA_DATABASE)",
        ok=bool(chroma_database),
        detail=chroma_database if chroma_database else "not set",
        fatal=True,
    )
    if not chroma_database:
        r.fix_suggestions = [
            "nx config init                           (interactive wizard, also provisions database)",
            "nx config set chroma_database <name>",
            "e.g. nx config set chroma_database nexus",
        ]
    results.append(r)

    # ChromaDB reachability
    if chroma_key and chroma_database:
        try:
            chromadb.CloudClient(
                tenant=chroma_tenant or None, database=chroma_database, api_key=chroma_key
            )
            results.append(HealthResult(
                label=f"ChromaDB  ({chroma_database})", ok=True, detail="reachable",
            ))
        except Exception as exc:
            _log.debug("db_not_reachable", db_name=chroma_database, error=str(exc))
            results.append(HealthResult(
                label=f"ChromaDB  ({chroma_database})",
                ok=False,
                detail="not reachable",
                fix_suggestions=[
                    "Run 'nx config init' to provision the database automatically.",
                    f"Or create '{chroma_database}' manually in the ChromaDB Cloud dashboard.",
                ],
                fatal=True,
            ))
        # Old layout warning (skip if migrated flag is set)
        migrated = get_credential("migrated")
        if not migrated:
            try:
                chromadb.CloudClient(
                    tenant=chroma_tenant or None, database=f"{chroma_database}_code", api_key=chroma_key
                )
                results.append(HealthResult(
                    label=f"ChromaDB  ({chroma_database}_code)",
                    ok=False,
                    detail="old layout detected — migrate and set NX_MIGRATED=1",
                ))
            except Exception:
                pass

    # VOYAGE_API_KEY
    voyage_key = get_credential("voyage_api_key")
    r = HealthResult(
        label="Voyage AI (VOYAGE_API_KEY)",
        ok=bool(voyage_key),
        detail="set" if voyage_key else "not set",
        fatal=True,
    )
    if not voyage_key:
        r.fix_suggestions = [
            "nx config init                           (interactive wizard)",
            "nx config set voyage_api_key <your-key>  (set individually)",
            "Get key: https://voyageai.com  →  Dashboard  →  API Keys",
        ]
    results.append(r)

    # Pipeline version check
    if chroma_key and chroma_database and voyage_key:
        from nexus.indexer import PIPELINE_VERSION, get_collection_pipeline_version

        stale_count = 0
        pipeline_results: list[HealthResult] = []
        try:
            client = chromadb.CloudClient(
                tenant=chroma_tenant or None, database=chroma_database, api_key=chroma_key
            )
            cols = client.list_collections()
            for col in cols:
                stored = get_collection_pipeline_version(col)
                if stored is None:
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=True,
                        detail="no version stamp (index with --force to stamp)",
                    ))
                elif stored != PIPELINE_VERSION:
                    stale_count += 1
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=False,
                        detail=f"v{stored} (current: v{PIPELINE_VERSION})",
                    ))
                else:
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=True, detail=f"v{stored}",
                    ))
        except Exception as exc:
            _log.debug("doctor_pipeline_check_failed", db=chroma_database, error=str(exc))
            pipeline_results.append(HealthResult(
                label=f"pipeline ({chroma_database})", ok=False, detail="check failed",
            ))

        # Add fix suggestions to the last pipeline result if stale
        if stale_count and pipeline_results:
            pipeline_results[-1].fix_suggestions = [
                "nx index repo <path> --force-stale  (re-index outdated collections)",
                "nx index repo <path> --force        (re-index all collections)",
            ]
            pipeline_results[-1].fatal = True

        results.extend(pipeline_results)

    return results


def _check_tools() -> list[HealthResult]:
    results: list[HealthResult] = []

    # ripgrep
    rg_path = shutil.which("rg")
    r = HealthResult(
        label="ripgrep   (rg)",
        ok=bool(rg_path),
        detail=rg_path or "not found — hybrid search disabled",
        fatal=True,
    )
    if not rg_path:
        r.fix_suggestions = [
            "brew install ripgrep                      (macOS)",
            "apt install ripgrep                       (Ubuntu/Debian)",
            "https://github.com/BurntSushi/ripgrep#installation",
        ]
    results.append(r)

    # git
    git_path = shutil.which("git")
    r = HealthResult(
        label="git",
        ok=bool(git_path),
        detail=git_path or "not found on PATH",
        fatal=True,
    )
    if not git_path:
        r.fix_suggestions = [
            "brew install git                          (macOS)",
            "apt install git                           (Ubuntu/Debian)",
            "https://git-scm.com/downloads",
        ]
    results.append(r)

    # bd (beads, optional)
    bd_path = shutil.which("bd")
    if bd_path:
        results.append(HealthResult(label="bd (beads, optional)", ok=True, detail=bd_path))
    else:
        results.append(HealthResult(
            label="bd (beads, optional)",
            ok=True,
            detail="not found — task tracking unavailable",
            fix_suggestions=["https://github.com/BeadsProject/beads"],
        ))

    # npx (Node.js, plugin-only)
    # Required by the nx Claude Code plugin, which spawns the
    # ``sequential-thinking`` and ``context7`` MCP servers via ``npx -y …``.
    # The CLI alone does not need it, so this is non-fatal — but a missing
    # ``npx`` causes silent MCP-server failures the moment a plugin tool is
    # invoked. Reported as informational so plugin users see the gap before
    # they hit it at runtime.
    npx_path = shutil.which("npx")
    if npx_path:
        results.append(HealthResult(label="npx (Node.js, plugin-only)", ok=True, detail=npx_path))
    else:
        results.append(HealthResult(
            label="npx (Node.js, plugin-only)",
            ok=True,
            detail="not found — plugin MCP servers (sequential-thinking, context7) will fail",
            fix_suggestions=[
                "brew install node                         (macOS)",
                "apt install nodejs npm                    (Ubuntu/Debian)",
                "https://nodejs.org/                       (other platforms)",
            ],
        ))

    return results


def _check_git_hooks() -> list[HealthResult]:
    from nexus.commands.hooks import _effective_hooks_dir, SENTINEL_BEGIN
    from nexus.registry import RepoRegistry

    from nexus.config import nexus_config_dir

    results: list[HealthResult] = []
    hook_names = ("post-commit", "post-merge", "post-rewrite")
    registry_path = nexus_config_dir() / "repos.json"

    try:
        reg = RepoRegistry(registry_path)
        repos = reg.all()
    except Exception as exc:
        _log.warning("doctor_registry_load_failed", error=str(exc))
        repos = []

    if not repos:
        results.append(HealthResult(
            label="git hooks", ok=True,
            detail="no repos registered — run: nx index repo <path>",
        ))
    else:
        for repo_str in repos:
            repo_path = Path(repo_str)
            try:
                hdir = _effective_hooks_dir(repo_path)
                installed = [
                    n for n in hook_names
                    if (hdir / n).exists() and SENTINEL_BEGIN in (hdir / n).read_text()
                ]
                if installed:
                    results.append(HealthResult(
                        label="git hooks", ok=True,
                        detail=f"{repo_path} ({', '.join(installed)})",
                    ))
                else:
                    results.append(HealthResult(
                        label="git hooks", ok=True,
                        detail=f"{repo_path} — not installed",
                        fix_suggestions=[f"nx hooks install {repo_path}"],
                    ))
            except Exception:
                results.append(HealthResult(
                    label="git hooks", ok=True,
                    detail=f"{repo_path} — could not check",
                ))

    return results


def _check_index_log() -> list[HealthResult]:
    from nexus.config import nexus_config_dir

    log_path = nexus_config_dir() / "index.log"
    if log_path.exists():
        mtime = log_path.stat().st_mtime
        age_s = time.time() - mtime
        if age_s < 60:
            age_str = f"{int(age_s)}s ago"
        elif age_s < 3600:
            age_str = f"{int(age_s // 60)} minutes ago"
        else:
            age_str = f"{int(age_s // 3600)} hours ago"
        return [HealthResult(
            label="index log", ok=True,
            detail=f"{log_path} (last write: {age_str})",
        )]
    return [HealthResult(
        label="index log", ok=True,
        detail=f"{log_path} (not created yet — git hooks have not fired)",
    )]


def _check_orphan_t1() -> list[HealthResult]:
    """Report on session files. An "orphan" here means the chroma server PID
    in the file is no longer alive — that is the only state that warrants
    cleanup. Live-but-unowned sessions (e.g. server still running after the
    spawning Claude Code instance exited) are listed but not flagged.
    """
    from nexus.session import SESSIONS_DIR

    if not SESSIONS_DIR.exists():
        return [HealthResult(label="T1 sessions", ok=True, detail="no sessions directory")]

    session_files = list(SESSIONS_DIR.glob("*.session"))
    if not session_files:
        return [HealthResult(label="T1 sessions", ok=True, detail="no session files")]

    now = time.time()
    orphans: list[str] = []
    live_descriptors: list[str] = []
    for sf in session_files:
        try:
            record = json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            _log.debug("orphan_t1_session_corrupt", path=str(sf), error=str(exc))
            continue
        pid = record.get("server_pid")
        age_s = max(0, int(now - sf.stat().st_mtime))
        age_str = f"{age_s // 60}m" if age_s < 3600 else f"{age_s // 3600}h"
        if pid is None:
            live_descriptors.append(f"{sf.name} (no pid, age {age_str})")
            continue
        try:
            os.kill(int(pid), 0)
            live_descriptors.append(f"{sf.name} (pid {pid} alive, age {age_str})")
        except OSError:
            orphans.append(sf.name)
        except (ValueError, TypeError):
            _log.debug("orphan_t1_invalid_pid", path=str(sf), pid=repr(pid))
            continue

    if orphans:
        return [HealthResult(
            label="T1 sessions",
            ok=False,
            detail=f"{len(orphans)} orphaned session file(s) (chroma pid dead): {', '.join(orphans)}",
            fix_suggestions=[
                "Remove stale files: rm ~/.config/nexus/sessions/*.session",
                "Or run: nx doctor (sweep runs automatically on session start)",
            ],
        )]

    return [HealthResult(
        label="T1 sessions", ok=True,
        detail=f"{len(session_files)} session file(s), all chroma servers alive: {', '.join(live_descriptors)}",
    )]


def _check_orphan_checkpoints() -> list[HealthResult]:
    from nexus.checkpoint import CHECKPOINT_DIR, scan_orphaned_checkpoints

    if not CHECKPOINT_DIR.exists():
        return [HealthResult(label="PDF checkpoints", ok=True, detail="no checkpoint directory")]

    try:
        orphans = scan_orphaned_checkpoints(delete=False)
    except Exception as exc:
        _log.debug("orphan_checkpoint_scan_failed", error=str(exc))
        return [HealthResult(label="PDF checkpoints", ok=True, detail="scan failed — skipping")]

    total = len(list(CHECKPOINT_DIR.glob("*.json")))
    if orphans:
        return [HealthResult(
            label="PDF checkpoints",
            ok=False,
            detail=f"{len(orphans)} orphaned checkpoint(s) out of {total} total",
            fix_suggestions=["Remove stale checkpoints: nx doctor --clean-checkpoints"],
        )]

    return [HealthResult(
        label="PDF checkpoints", ok=True,
        detail=f"{total} checkpoint(s), none orphaned" if total else "no checkpoints",
    )]


def _check_orphan_pipelines() -> list[HealthResult]:
    from nexus.pipeline_buffer import PIPELINE_DB_PATH, PipelineDB

    if not PIPELINE_DB_PATH.exists():
        return [HealthResult(label="PDF pipeline buffer", ok=True, detail="no pipeline database")]

    try:
        db = PipelineDB(PIPELINE_DB_PATH)
        orphans = db.scan_orphaned_pipelines(delete=False)
    except Exception as exc:
        _log.debug("orphan_pipeline_scan_failed", error=str(exc))
        return [HealthResult(label="PDF pipeline buffer", ok=True, detail="scan failed — skipping")]

    total = db.count_pipelines()
    if orphans:
        return [HealthResult(
            label="PDF pipeline buffer",
            ok=False,
            detail=f"{len(orphans)} orphaned entry/entries out of {total} total",
            fix_suggestions=["Remove stale entries: nx doctor --clean-pipelines"],
        )]

    return [HealthResult(
        label="PDF pipeline buffer", ok=True,
        detail=f"{total} entry/entries, none orphaned" if total else "empty",
    )]


def _check_t2_integrity() -> list[HealthResult]:
    db_path = default_db_path()
    if not db_path.exists():
        return [HealthResult(label="T2 integrity", ok=True, detail="not created yet")]

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            pragma_ok = len(rows) == 1 and rows[0][0] == "ok"
            if not pragma_ok:
                issues = "; ".join(r[0] for r in rows[:3])
                return [HealthResult(label="T2 integrity", ok=False, detail=f"PRAGMA: {issues}")]

            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')")
            fts_ok = True
        except sqlite3.OperationalError as exc:
            return [HealthResult(label="T2 integrity", ok=False, detail=f"FTS5: {exc}")]
        finally:
            conn.close()
    except Exception as exc:
        return [HealthResult(label="T2 integrity", ok=False, detail=f"could not open: {exc}")]

    if pragma_ok and fts_ok:
        return [HealthResult(label="T2 integrity", ok=True, detail="PRAGMA ok, FTS5 ok")]
    return [HealthResult(label="T2 integrity", ok=False, detail="check failed")]


def _check_chroma_pagination(client: object, db_name: str) -> list[HealthResult]:
    try:
        cols = client.list_collections()  # type: ignore[union-attr]
    except Exception as exc:
        return [HealthResult(
            label=f"ChromaDB pagination ({db_name})", ok=False, detail=f"list failed: {exc}",
        )]

    target_col = None
    for col in cols:
        try:
            if col.count() > 0:
                target_col = col
                break
        except Exception:
            continue

    if target_col is None:
        return [HealthResult(
            label=f"ChromaDB pagination ({db_name})", ok=True,
            detail="no non-empty collections to audit",
        )]

    try:
        expected = target_col.count()
        retrieved = 0
        offset = 0
        page_size = 300
        while True:
            batch = target_col.get(limit=page_size, offset=offset, include=[])
            ids = batch.get("ids", [])
            retrieved += len(ids)
            if len(ids) < page_size:
                break
            offset += page_size

        ok = retrieved == expected
        detail = f"{target_col.name}: count={expected}, paginated={retrieved}"
        return [HealthResult(label=f"ChromaDB pagination ({db_name})", ok=ok, detail=detail)]
    except Exception as exc:
        return [HealthResult(
            label=f"ChromaDB pagination ({db_name})", ok=False, detail=f"audit failed: {exc}",
        )]


def _check_catalog() -> list[HealthResult]:
    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if Catalog.is_initialized(cat_path):
            cat = Catalog(cat_path, cat_path / ".catalog.db")
            doc_count = cat._db.execute("SELECT count(*) FROM documents").fetchone()[0]
            link_count = cat._db.execute("SELECT count(*) FROM links").fetchone()[0]
            return [HealthResult(
                label="Catalog", ok=True,
                detail=f"{doc_count} documents, {link_count} links at {cat_path}",
            )]
        return [HealthResult(
            label="Catalog", ok=True,
            detail="not initialized (optional — run: nx catalog setup)",
        )]
    except Exception:
        return [HealthResult(label="Catalog", ok=True, detail="check failed (non-critical)")]


# ── Orchestrator ──────────────────────────────────────────────────────────────


def run_health_checks() -> tuple[list[HealthResult], bool]:
    """Run all health checks.

    Returns (results, is_local_mode).
    """
    from nexus.config import is_local_mode, get_credential

    results: list[HealthResult] = []

    results.extend(_check_python())
    results.extend(_check_cli_version())

    _local = is_local_mode()
    if _local:
        results.extend(_check_t3_local())
    else:
        results.extend(_check_t3_cloud())

    results.extend(_check_tools())
    results.extend(_check_git_hooks())
    results.extend(_check_index_log())
    results.extend(_check_orphan_t1())
    results.extend(_check_orphan_checkpoints())
    results.extend(_check_orphan_pipelines())
    results.extend(_check_t2_integrity())

    # ChromaDB pagination audit (cloud only)
    if not _local:
        chroma_key = get_credential("chroma_api_key")
        chroma_database = get_credential("chroma_database")
        chroma_tenant = get_credential("chroma_tenant")
        if chroma_key and chroma_database:
            try:
                client = chromadb.CloudClient(
                    tenant=chroma_tenant or None, database=chroma_database, api_key=chroma_key
                )
                results.extend(_check_chroma_pagination(client, chroma_database))
            except Exception as exc:
                _log.debug(
                    "doctor_pagination_check_client_failed",
                    db=chroma_database, error=str(exc),
                )
                results.append(HealthResult(
                    label=f"ChromaDB pagination ({chroma_database})", ok=True,
                    detail="skipped (client unavailable)",
                ))

    results.extend(_check_catalog())

    return results, _local
