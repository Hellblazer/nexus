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
from typing import TYPE_CHECKING

import chromadb
import structlog

from nexus.config import default_db_path

if TYPE_CHECKING:
    from nexus.catalog import Catalog

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
            "brew install python@3.12                                 (macOS)",
            "apt install python3.12                                   (Ubuntu/Debian)",
            "winget install --id Python.Python.3.12 --scope user      (Windows)",
            "https://www.python.org/downloads/",
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
            # RDR-120 P6 (nexus-qg86h): direct mode decommissioned. The
            # local-mode probe always routes through the T3 daemon's
            # HttpClient; the legacy PersistentClient direct-open
            # branch is deleted. (chromadb's WAL races between
            # processes when two PersistentClients open the same store
            # simultaneously, which is why the daemon path was added
            # at P2 in the first place; P6 makes it the only path.)
            from nexus.daemon.t3_client import make_t3_client

            client = make_t3_client()._client
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
            # RDR-120 P2: route through make_t3 so the reachability
            # probe exercises the same code path the indexer takes.
            # Daemon mode is local-only; this branch only fires in
            # cloud mode, so make_t3 dispatches to CloudClient.
            from nexus.db import make_t3
            make_t3()
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
        # RDR-037 transitional probe for the legacy four-database
        # layout was retired post-migration; the single-database
        # architecture is the only supported shape.

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
            # RDR-120 P2: route through make_t3 for the cloud-mode
            # pipeline-version sweep. Cloud-only branch; daemon does
            # not apply.
            from nexus.db import make_t3
            client = make_t3()._client
            cols = client.list_collections()
            for col in cols:
                # taxonomy__* collections are BERTopic aggregates (RDR-070),
                # not indexer outputs — PIPELINE_VERSION does not apply.
                if col.name.startswith("taxonomy__"):
                    continue
                stored = get_collection_pipeline_version(col)
                if stored is None:
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=True,
                        detail="no version stamp (next 'nx index repo' will stamp)",
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
        # nexus-njmg (GH #622): winget --scope user avoids UAC-prompt
        # failures during unattended install on Windows.
        r.fix_suggestions = [
            "brew install ripgrep                                          (macOS)",
            "apt install ripgrep                                           (Ubuntu/Debian)",
            "winget install --id BurntSushi.ripgrep.MSVC --scope user      (Windows)",
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
            "brew install git                                              (macOS)",
            "apt install git                                               (Ubuntu/Debian)",
            "winget install --id Git.Git --scope user                      (Windows)",
            "https://git-scm.com/downloads",
        ]
    results.append(r)

    # bd (beads, optional)
    bd_path = shutil.which("bd")
    if bd_path:
        results.append(HealthResult(label="bd (beads, optional)", ok=True, detail=bd_path))
    else:
        # bd has no winget package (verified 2026-05-10); upstream releases
        # ship as a GitHub release zip operators install manually.
        results.append(HealthResult(
            label="bd (beads, optional)",
            ok=True,
            detail="not found — task tracking unavailable",
            fix_suggestions=[
                "https://github.com/BeadsProject/beads/releases   (download for your OS)",
            ],
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
                "brew install node                                              (macOS)",
                "apt install nodejs npm                                         (Ubuntu/Debian)",
                "winget install --id OpenJS.NodeJS.LTS --scope user             (Windows)",
                "https://nodejs.org/                                            (other platforms)",
            ],
        ))

    return results


def _check_git_hooks() -> list[HealthResult]:
    # nexus-8g79.10 (V2): import from the lower-layer module instead of
    # reaching up into commands/. Use module-attribute access so test
    # monkeypatches on ``nexus._git_hooks_meta.effective_hooks_dir``
    # reach the live binding at call time.
    import re
    from nexus import _git_hooks_meta as _ghm
    from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END
    _effective_hooks_dir = _ghm.effective_hooks_dir
    from nexus.registry import RepoRegistry

    from nexus.config import nexus_config_dir

    results: list[HealthResult] = []
    hook_names = ("post-commit", "post-merge", "post-rewrite")
    registry_path = nexus_config_dir() / "repos.json"

    # nexus-mkj6u shakeout: extract the canonical stanza from the
    # current template so we can detect drift in already-installed
    # hooks (e.g. the pre-pgrep-guard stanza). Done once per call;
    # the import is lazy because commands/hooks.py imports click
    # which we don't want to pay for at health-check time when no
    # repos are registered.
    def _canonical_stanza_body() -> str | None:
        try:
            from nexus.commands.hooks import _STANZA
        except Exception:
            return None
        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}",
            _STANZA, re.DOTALL,
        )
        return m.group(1) if m else None

    def _installed_stanza_body(content: str) -> str | None:
        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}",
            content, re.DOTALL,
        )
        return m.group(1) if m else None

    canonical = _canonical_stanza_body()

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
                    # nexus-mkj6u: drift check — compare installed stanza
                    # body to the canonical template body. Different
                    # body means the user is running an old stanza
                    # (e.g. pre-pgrep-guard, vulnerable to the multi-
                    # indexer pile-up race).
                    drifted: list[str] = []
                    if canonical is not None:
                        for name in installed:
                            installed_body = _installed_stanza_body(
                                (hdir / name).read_text()
                            )
                            if installed_body is not None and installed_body != canonical:
                                drifted.append(name)
                    if drifted:
                        results.append(HealthResult(
                            label="git hooks (stanza drift)",
                            ok=False,
                            detail=(
                                f"{repo_path} — installed stanza differs from "
                                f"current template ({', '.join(drifted)}). "
                                "May be missing pile-up guard or other fixes."
                            ),
                            fix_suggestions=[f"nx hooks update {repo_path}"],
                            fatal=False,
                        ))
                    else:
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
    """Report on T1 address files left by previous Claude Code sessions.

    RDR-105 P4 replaced the per-session JSON record files with a
    single-writer ``~/.config/nexus/t1_addr.<claude_pid>`` flat file
    per live Claude. An orphan here is an addr file whose
    ``<claude_pid>`` is no longer a running process (typically left
    behind when Claude Code exits ungracefully so the lifespan
    finally never ran). The MCP startup sweep
    (``sweep_orphan_t1_addr_files``) reaps them automatically; this
    check just surfaces what is currently on disk.
    """
    from nexus.config import nexus_config_dir

    config_dir = nexus_config_dir()
    if not config_dir.exists():
        return [HealthResult(label="T1 sessions", ok=True, detail="no nexus config dir")]

    addr_files = list(config_dir.glob("t1_addr.*"))
    if not addr_files:
        return [HealthResult(label="T1 sessions", ok=True, detail="no live T1 sessions")]

    now = time.time()
    orphans: list[str] = []
    live_descriptors: list[str] = []
    for path in addr_files:
        suffix = path.suffix.lstrip(".")
        try:
            claude_pid = int(suffix)
        except ValueError:
            _log.debug("orphan_t1_addr_unparseable_suffix", path=str(path), suffix=suffix)
            continue
        age_s = max(0, int(now - path.stat().st_mtime))
        age_str = f"{age_s // 60}m" if age_s < 3600 else f"{age_s // 3600}h"
        try:
            os.kill(claude_pid, 0)
            live_descriptors.append(f"{path.name} (claude_pid {claude_pid} alive, age {age_str})")
        except OSError:
            orphans.append(path.name)

    if orphans:
        return [HealthResult(
            label="T1 sessions",
            ok=False,
            detail=f"{len(orphans)} orphan addr file(s) (claude_pid dead): {', '.join(orphans)}",
            fix_suggestions=[
                "Remove stale files: rm ~/.config/nexus/t1_addr.*",
                "Or run nx doctor (the next MCP startup sweeps these automatically).",
            ],
        )]

    return [HealthResult(
        label="T1 sessions", ok=True,
        detail=f"{len(addr_files)} addr file(s), all owning Claudes alive: {', '.join(live_descriptors)}",
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


def _check_mineru_server() -> list[HealthResult]:
    """nexus-h1jk: surface MinerU server reachability in the default
    doctor flow.

    Math-heavy PDFs (papers with dense formula notation) accumulate per-
    page tensor state in MinerU's formula-detection pass and routinely
    OOM-kill the in-process subprocess fallback. The HTTP server avoids
    that by running MinerU as a long-lived dedicated worker. The
    configured URL silently goes stale: ``_restart_mineru_server`` in
    ``pdf_extractor.py`` writes the live port to
    ``~/.config/nexus/config.yml`` after a mid-run recovery, but if
    that server later dies the URL points at a dead port across every
    subsequent session. ``nx doctor`` is the natural place to surface
    that drift.
    """
    from nexus.config import get_mineru_server_url
    import httpx as _httpx

    try:
        url = get_mineru_server_url()
    except Exception:
        return []
    if not url:
        return []

    health_url = f"{url}/health"
    try:
        resp = _httpx.get(health_url, timeout=2.0)
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        return [HealthResult(
            label="MinerU server",
            ok=False,
            detail=(
                f"{url} unreachable ({type(exc).__name__}); falling back to "
                "in-process subprocess on math PDFs (OOM-risk)"
            ),
            fix_suggestions=[
                "Start the server: nx mineru start",
                f"Or confirm the URL in ~/.config/nexus/config.yml "
                f"(currently: {url})",
            ],
        )]
    if resp.status_code != 200:
        return [HealthResult(
            label="MinerU server",
            ok=False,
            detail=f"{url} returned HTTP {resp.status_code}",
            fix_suggestions=["Restart the server: nx mineru stop && nx mineru start"],
        )]
    return [HealthResult(
        label="MinerU server",
        ok=True,
        detail=f"reachable at {url}",
    )]


def _check_t2_integrity() -> list[HealthResult]:
    db_path = default_db_path()
    if not db_path.exists():
        return [HealthResult(label="T2 integrity", ok=True, detail="not created yet")]

    try:
        conn = sqlite3.connect(str(db_path))  # epsilon-allow: health PRAGMA integrity_check diagnostic — must operate when daemon offline; read-only
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


def _check_catalog(cat: "Catalog | None", cat_path: "Path") -> list[HealthResult]:
    try:
        if cat is not None:
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


def _check_plugin_name() -> list[HealthResult]:
    """nexus-mkj6u: warn when the installed Claude Code plugin's name
    differs from what the CLI expects.

    The 2026-05-23 rename moved the plugin name from ``nx`` to
    ``conexus``. Claude Code does NOT auto-uninstall renamed plugins;
    a user's local cache at
    ``~/.claude/plugins/cache/nexus-plugins/nx/...`` survives the
    marketplace.json rename. Until they explicitly uninstall +
    reinstall, they run the NEW conexus CLI under the OLD ``nx``
    plugin. The MCP-server-startup check fires once per session;
    this doctor check is the explicit-invocation surface for users
    who run ``nx doctor`` to diagnose what's stale.

    Non-fatal. Returns an empty list when no ``CLAUDE_PLUGIN_ROOT``
    is set (CLI-only use; nothing to check) or when the plugin name
    matches.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return []
    manifest_path = Path(plugin_root) / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text())
        plugin_name = manifest.get("name")
    except (OSError, json.JSONDecodeError):
        return []
    if not plugin_name:
        return []

    from nexus.mcp_infra import EXPECTED_PLUGIN_NAME
    if plugin_name == EXPECTED_PLUGIN_NAME:
        return []

    return [
        HealthResult(
            label="Claude Code plugin name (renamed)",
            ok=False,
            detail=(
                f"installed plugin is '{plugin_name}@nexus-plugins'; CLI "
                f"expects '{EXPECTED_PLUGIN_NAME}@nexus-plugins' "
                "(renamed 2026-05-23, nexus-mkj6u)"
            ),
            fix_suggestions=[
                f"/plugin uninstall {plugin_name}@nexus-plugins",
                f"/plugin install {EXPECTED_PLUGIN_NAME}@nexus-plugins",
                "(both commands run in Claude Code, not from the shell)",
            ],
            fatal=False,
        )
    ]


def _check_credential_persistence() -> list[HealthResult]:
    """nexus-m7evs: warn when cloud credentials live in shell env only.

    GUI-spawned ``nx-mcp`` (Claude Desktop, Cowork SDK bridge) inherits
    launchd's environment, NOT the user's interactive shell. If
    ``CHROMA_API_KEY`` / ``VOYAGE_API_KEY`` are in ``.zshrc`` exports
    but never persisted via ``nx config set``, the GUI-spawned
    subprocess sees them as absent, ``is_local_mode()`` flips to True,
    and T3 dispatch goes to the daemon path that fails opaquely.

    This check runs on the CLI side (where shell env IS visible) and
    surfaces the gap before the GUI-spawn path hits it. Non-fatal: a
    warning, not a blocker, because the CLI itself works fine.

    Returns an empty list when the configuration is consistent (both
    persisted, neither set, or no env exports).
    """
    from nexus.config import _global_config_path

    cloud_keys = ("chroma_api_key", "voyage_api_key", "chroma_tenant", "chroma_database")
    env_names = {
        "chroma_api_key": "CHROMA_API_KEY",
        "voyage_api_key": "VOYAGE_API_KEY",
        "chroma_tenant": "CHROMA_TENANT",
        "chroma_database": "CHROMA_DATABASE",
    }

    # Read config.yml directly; we want to see file state independent of env.
    file_creds: dict[str, str] = {}
    cfg_path = _global_config_path()
    if cfg_path.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg_path.read_text()) or {}
            file_creds = data.get("credentials", {}) or {}
        except Exception:
            file_creds = {}

    env_only: list[str] = []
    for key in cloud_keys:
        env_present = bool(os.environ.get(env_names[key], "").strip())
        file_present = bool(str(file_creds.get(key, "")).strip())
        if env_present and not file_present:
            env_only.append(key)

    if not env_only:
        return []

    # Surface the most-load-bearing pair first; chroma_tenant /
    # chroma_database are derived/configuration rather than identity.
    suggestions = [f"nx config set {key} \"${env_names[key]}\"" for key in env_only]
    suggestions.append(
        "Then quit and relaunch Claude Desktop so the next nx-mcp "
        "spawn reads ~/.config/nexus/config.yml instead of empty env."
    )

    detail = (
        f"{len(env_only)} credential(s) in shell env only: {', '.join(env_only)}. "
        "GUI-spawned consumers (Claude Desktop, Cowork) cannot see "
        "shell env vars and will misdetect cloud mode as local mode."
    )

    return [
        HealthResult(
            label="Credential persistence (GUI spawn)",
            ok=False,
            detail=detail,
            fix_suggestions=suggestions,
            fatal=False,
        )
    ]


def run_health_checks() -> tuple[list[HealthResult], bool]:
    """Run all health checks.

    Returns (results, is_local_mode).
    """
    from nexus.config import is_local_mode, get_credential

    results: list[HealthResult] = []

    results.extend(_check_python())
    results.extend(_check_cli_version())
    results.extend(_check_plugin_name())
    results.extend(_check_credential_persistence())

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
    results.extend(_check_mineru_server())
    results.extend(_check_t2_integrity())

    # ChromaDB pagination audit (cloud only)
    if not _local:
        chroma_key = get_credential("chroma_api_key")
        chroma_database = get_credential("chroma_database")
        chroma_tenant = get_credential("chroma_tenant")
        if chroma_key and chroma_database:
            try:
                # RDR-120 P2: route through make_t3. Cloud-only branch
                # (gated by ``not _local``); daemon does not apply.
                from nexus.db import make_t3
                client = make_t3()._client
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

    from nexus.catalog import Catalog
    from nexus.config import catalog_path
    _cat_path = catalog_path()
    _cat = (
        Catalog(_cat_path, _cat_path / ".catalog.db")
        if Catalog.is_initialized(_cat_path)
        else None
    )
    results.extend(_check_catalog(_cat, _cat_path))

    return results, _local
