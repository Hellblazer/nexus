# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
import shutil
import sys

import chromadb
import click
import structlog

from nexus.config import get_credential
from nexus.db.t3 import _STORE_TYPES

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
             "nx config set chroma_api_key <your-key>",
             "Get key: https://trychroma.com")

    # ── CHROMA_TENANT ─────────────────────────────────────────────────────────
    chroma_tenant = get_credential("chroma_tenant")
    lines.append(_check_line("ChromaDB  (CHROMA_TENANT)",   bool(chroma_tenant),
                              "set" if chroma_tenant else "not set"))
    if not chroma_tenant:
        failed = True
        _fix(lines,
             "nx config set chroma_tenant <your-tenant>",
             "Get value: https://trychroma.com  (Dashboard → Settings)")

    # ── CHROMA_DATABASE ───────────────────────────────────────────────────────
    chroma_database = get_credential("chroma_database")
    lines.append(_check_line("ChromaDB  (CHROMA_DATABASE)", bool(chroma_database),
                              chroma_database if chroma_database else "not set"))
    if not chroma_database:
        failed = True
        _fix(lines,
             "nx config set chroma_database <your-database>",
             "Get value: https://trychroma.com  (Dashboard → Settings)")

    # ── ChromaDB four-store databases ────────────────────────────────────────
    if chroma_key and chroma_tenant and chroma_database:
        db_ok = True
        for t in _STORE_TYPES:
            db_name = f"{chroma_database}_{t}"
            try:
                chromadb.CloudClient(
                    tenant=chroma_tenant, database=db_name, api_key=chroma_key
                )
                lines.append(_check_line(f"ChromaDB  ({db_name})", True, "reachable"))
            except Exception as exc:
                db_ok = False
                failed = True
                _log.debug("db_not_reachable", db_name=db_name, error=str(exc))
                lines.append(_check_line(f"ChromaDB  ({db_name})", False, "not reachable"))
        if not db_ok:
            _fix(lines,
                 f"Create these databases in your ChromaDB Cloud dashboard:",
                 *[f"  - {chroma_database}_{t}" for t in _STORE_TYPES],
                 "Then run: nx migrate t3  (to copy data from the old single store)")

    # ── VOYAGE_API_KEY ────────────────────────────────────────────────────────
    voyage_key = get_credential("voyage_api_key")
    lines.append(_check_line("Voyage AI (VOYAGE_API_KEY)",  bool(voyage_key),
                              "set" if voyage_key else "not set"))
    if not voyage_key:
        failed = True
        _fix(lines,
             "nx config set voyage_api_key <your-key>",
             "Get key: https://www.voyageai.com")

    # ── ANTHROPIC_API_KEY ─────────────────────────────────────────────────────
    anthropic_key = get_credential("anthropic_api_key")
    lines.append(_check_line("Anthropic (ANTHROPIC_API_KEY)", bool(anthropic_key),
                              "set" if anthropic_key else "not set"))
    if not anthropic_key:
        failed = True
        _fix(lines,
             "nx config set anthropic_api_key <your-key>",
             "Get key: https://console.anthropic.com")

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

    # ── Nexus server ──────────────────────────────────────────────────────────
    # Server is optional for most commands; report status but do not fail.
    from nexus.commands.serve import _read_pid, _process_running
    pid = _read_pid()
    server_running = pid is not None and _process_running(pid)
    lines.append(_check_line("Nexus server",                server_running,
                              f"running (PID {pid})" if server_running else
                              "not running (optional — needed for search-over-HTTP)"))
    if not server_running:
        _fix(lines, "nx serve start")

    # ── Mixedbread (optional) ─────────────────────────────────────────────────
    mxbai_key = get_credential("mxbai_api_key")
    lines.append(_check_line("Mixedbread (MXBAI_API_KEY, optional)", True,
                              "set" if mxbai_key else "not set — only needed for --mxbai flag"))

    click.echo("\n".join(lines))

    if failed:
        click.echo("\nRun 'nx config init' for an interactive setup wizard.")
        raise click.exceptions.Exit(1)
