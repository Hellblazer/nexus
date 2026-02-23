# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
import shutil

import click

from nexus.config import get_credential

_CHECK = "✓"
_WARN  = "✗"

_SIGNUP = {
    "CHROMA_API_KEY":      "https://trychroma.com",
    "CHROMA_TENANT":       "https://trychroma.com",
    "CHROMA_DATABASE":     "https://trychroma.com",
    "VOYAGE_API_KEY":      "https://voyageai.com",
    "ANTHROPIC_API_KEY":   "https://console.anthropic.com",
    "MXBAI_API_KEY":       "https://mixedbread.ai",
}


def _check(label: str, ok: bool, detail: str = "") -> str:
    status = _CHECK if ok else _WARN
    msg = f"  {status} {label}"
    if detail:
        msg += f": {detail}"
    return msg


@click.command("doctor")
def doctor_cmd() -> None:
    """Verify that all required services and credentials are available."""
    lines: list[str] = ["Nexus health check:\n"]
    missing: list[str] = []
    missing_tools: list[str] = []

    # CHROMA_API_KEY
    chroma_key = get_credential("chroma_api_key")
    lines.append(_check("ChromaDB  (CHROMA_API_KEY)",  bool(chroma_key),
                        "set" if chroma_key else "not set"))
    if not chroma_key:
        missing.append("CHROMA_API_KEY")

    # CHROMA_TENANT
    chroma_tenant = get_credential("chroma_tenant")
    lines.append(_check("ChromaDB  (CHROMA_TENANT)",   bool(chroma_tenant),
                        "set" if chroma_tenant else "not set"))
    if not chroma_tenant:
        missing.append("CHROMA_TENANT")

    # CHROMA_DATABASE
    chroma_database = get_credential("chroma_database")
    lines.append(_check("ChromaDB  (CHROMA_DATABASE)", bool(chroma_database),
                        "set" if chroma_database else "not set"))
    if not chroma_database:
        missing.append("CHROMA_DATABASE")

    # VOYAGE_API_KEY
    voyage_key = get_credential("voyage_api_key")
    lines.append(_check("Voyage AI (VOYAGE_API_KEY)",  bool(voyage_key),
                        "set" if voyage_key else "not set"))
    if not voyage_key:
        missing.append("VOYAGE_API_KEY")

    # ANTHROPIC_API_KEY
    anthropic_key = get_credential("anthropic_api_key")
    lines.append(_check("Anthropic (ANTHROPIC_API_KEY)", bool(anthropic_key),
                        "set" if anthropic_key else "not set"))
    if not anthropic_key:
        missing.append("ANTHROPIC_API_KEY")

    # ripgrep
    rg_path = shutil.which("rg")
    lines.append(_check("ripgrep   (rg)",              bool(rg_path),
                        rg_path or "not found on PATH — install ripgrep"))
    if not rg_path:
        missing_tools.append("rg")

    # git
    git_path = shutil.which("git")
    lines.append(_check("git",                         bool(git_path),
                        git_path or "not found on PATH"))
    if not git_path:
        missing_tools.append("git")

    # server running check
    from nexus.commands.serve import _read_pid, _process_running
    pid = _read_pid()
    server_running = pid is not None and _process_running(pid)
    lines.append(_check("Nexus server",                server_running,
                        f"running (PID {pid})" if server_running else "not running — use 'nx serve start'"))

    # Mixedbread (optional)
    mxbai_key = get_credential("mxbai_api_key")
    lines.append(_check("Mixedbread (MXBAI_API_KEY, optional)", True,
                        "set" if mxbai_key else "not set — only needed for --mxbai flag"))

    click.echo("\n".join(lines))

    if missing:
        click.echo("\nMissing credentials — get them here:")
        for key in missing:
            click.echo(f"  {key:<24} {_SIGNUP.get(key, '')}")
        click.echo("\nRun 'nx config init' for an interactive setup wizard.")

    if missing_tools:
        click.echo("\nMissing tools:")
        for tool in missing_tools:
            click.echo(f"  • {tool} — install via your system package manager")

    if missing or missing_tools:
        raise click.exceptions.Exit(1)
