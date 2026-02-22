# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
import os
import shutil

import click

_CHECK = "✓"
_WARN  = "✗"


def _check(label: str, ok: bool, detail: str = "") -> str:
    status = _CHECK if ok else _WARN
    msg = f"  {status} {label}"
    if detail:
        msg += f": {detail}"
    return msg


@click.command("doctor")
def doctor_cmd() -> None:
    """Verify that all required services and credentials are available."""
    lines: list[str] = ["Nexus health check:"]

    # CHROMA_API_KEY
    chroma_key = os.environ.get("CHROMA_API_KEY", "")
    lines.append(_check("ChromaDB (CHROMA_API_KEY)", bool(chroma_key),
                         "set" if chroma_key else "CHROMA_API_KEY not set"))

    # VOYAGE_API_KEY
    voyage_key = os.environ.get("VOYAGE_API_KEY", "")
    lines.append(_check("Voyage AI (VOYAGE_API_KEY)", bool(voyage_key),
                         "set" if voyage_key else "VOYAGE_API_KEY not set"))

    # ANTHROPIC_API_KEY
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    lines.append(_check("Anthropic (ANTHROPIC_API_KEY)", bool(anthropic_key),
                         "set" if anthropic_key else "ANTHROPIC_API_KEY not set"))

    # ripgrep
    rg_path = shutil.which("rg")
    lines.append(_check("ripgrep (rg)", bool(rg_path),
                         rg_path or "rg not found on PATH"))

    # git
    git_path = shutil.which("git")
    lines.append(_check("git", bool(git_path),
                         git_path or "git not found on PATH"))

    # Mixedbread (optional — only warn if MXBAI_API_KEY absent)
    mxbai_key = os.environ.get("MXBAI_API_KEY", "")
    lines.append(_check("Mixedbread (MXBAI_API_KEY, optional)", True,
                         "set" if mxbai_key else "MXBAI_API_KEY not set (--mxbai will warn)"))

    click.echo("\n".join(lines))
