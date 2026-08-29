# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-zjaht: `--help` must not name retired substrates.

The live 7.9.0 CLI told users `nx memory search` was "FTS5 keyword search"
(FTS5 retired at RDR-158) and that collections/t3 were "ChromaDB"
(retired at RDR-155 P4b) — the docs were right where the CLI itself was
wrong. Click renders a command function's docstring as its --help body,
so this lint walks every click-decorated function in src/nexus/commands/
and cli.py and fails if the docstring mentions a retired substrate.

Scope: DOCSTRINGS OF CLICK COMMANDS ONLY (the user-visible help surface).
Internal helpers may legitimately discuss Chroma/SQLite heritage; those
are out of scope here. A deliberate historical mention inside a click
docstring goes in the allowlist below with a reason.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

COMMANDS_DIR = Path(__file__).parent.parent / "src" / "nexus" / "commands"
CLI_PY = Path(__file__).parent.parent / "src" / "nexus" / "cli.py"

#: Retired-substrate tokens that must not appear in user-visible help.
#: sqlite is deliberately NOT banned wholesale: migration-source verbiage
#: ("migrates FROM SQLite") is legitimate help text during the retirement
#: era; chroma/fts5 have no such live role.
_BANNED = re.compile(r"chroma|fts5", re.IGNORECASE)

#: (file basename, function name) -> reason.
_ALLOWLIST: dict[tuple[str, str], str] = {
    # ``chroma`` here is a LIVE source_uri SCHEME value (chroma:// rows,
    # RDR-096 P3.2), not a claim about the storage substrate — the flag
    # genuinely filters rows whose persistent URI carries that scheme.
    ("enrich.py", "enrich_aspects_list"): "chroma:// is a live source_uri scheme",
    # Same scheme value: the census buckets rows by their URI scheme, and
    # chroma:// is one of the buckets (555 such rows live, nexus-mlu3k).
    ("enrich.py", "aspects_without_catalog_cmd"): "chroma:// is a live source_uri scheme (census bucket)",
}


def _is_click_decorated(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        text = ast.unparse(dec)
        if re.search(r"\bclick\.|\.command\(|\.group\(|^command\(|^group\(", text):
            return True
    return False


def _violations() -> list[str]:
    out: list[str] = []
    files = sorted(COMMANDS_DIR.glob("*.py")) + [CLI_PY]
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not _is_click_decorated(node):
                continue
            doc = ast.get_docstring(node) or ""
            m = _BANNED.search(doc)
            if not m:
                continue
            if (path.name, node.name) in _ALLOWLIST:
                continue
            out.append(f"{path.name}:{node.lineno} {node.name}: help says {m.group(0)!r}")
    return out


def test_no_click_help_names_a_retired_substrate() -> None:
    violations = _violations()
    assert violations == [], (
        "click --help text names a retired substrate (nexus-zjaht — the CLI "
        "lying about its own storage while the docs are correct):\n  "
        + "\n  ".join(violations)
        + "\nFix the docstring, or allowlist a deliberate historical mention "
        "with a reason."
    )


def test_sweep_is_not_vacuous() -> None:
    """The walker must actually find a real population of click commands —
    a broken decorator matcher returning zero commands would make the ban
    above pass over nothing."""
    count = 0
    for path in sorted(COMMANDS_DIR.glob("*.py")) + [CLI_PY]:
        tree = ast.parse(path.read_text(), filename=str(path))
        count += sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and _is_click_decorated(n)
        )
    assert count >= 50, f"only {count} click-decorated functions found — sweep broken?"
