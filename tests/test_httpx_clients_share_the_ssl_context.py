# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``httpx`` client in ``src/nexus`` is built on the process-wide SSL context.

``httpx.Client()`` and ``httpx.AsyncClient()`` build an SSL context eagerly,
reading the CA bundle (``certifi.where()``, inside the install generation the
process started from) at every construction. A long-lived process (the MCP
server, a daemon, the aspect worker) whose generation was removed then fails
its next client build with a pathless ``[Errno 2] No such file or directory``:
on 2026-11-21/22 that broke the T1 scratch tool for the rest of four sessions,
and review of the fix found the same build in ``IdentityClient`` (the MCP
identity lease builds one periodically), ``groups.py`` and more.

The rule: pass ``verify=get_shared_ssl_context()``
(``nexus.db.t2.shared_ssl``), or a custom ``transport=`` (no SSL context is
built), or be listed in ``ALLOWED`` with the reason the process is short-lived.
A stale ``ALLOWED`` entry fails too, and the scan must find at least
``MIN_SITES`` client constructions, so an import-shape change that hides them
all cannot pass vacuously.

Module-level request helpers (``httpx.get``, ``httpx.post``, ``httpx.stream``)
build a client per call as well and are not covered here.
"""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
CLIENT_CLASSES = frozenset({"Client", "AsyncClient"})
SHARED_CONTEXT_FUNCS = frozenset({"get_shared_ssl_context", "_get_shared_ssl_context"})

#: (path under src/nexus, enclosing function) -> why a per-client SSL context is acceptable there.
ALLOWED: dict[tuple[str, str], str] = {
    ("backup/cloud_export.py", "ExportClient.__init__"): "nx backup export: a CLI command, one process per run",
    ("backup/cloud_import.py", "ImportClient.__init__"): "nx backup import: a CLI command, one process per run",
    ("commands/catalog_cmds/backfill.py", "backfill_etags_cmd"): "nx catalog backfill-etags: a CLI command",
    ("commands/doctor.py", "_run_check_mineru"): "nx doctor: a CLI command, one process per run",
    ("doctor_references.py", "run_check_references"): "nx doctor --check-references: reached only from the CLI",
}

#: The scan found 19 constructions when this was written (2026-11-23).
MIN_SITES = 15


@cache
def _sites() -> tuple[tuple[str, str, int, str], ...]:
    """``(path, enclosing function, line, verdict)`` for every client construction;
    verdict is ``shared``, ``transport`` or ``per-client``."""
    found: list[tuple[str, str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_aliases = {"httpx"}
        class_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_aliases |= {a.asname or a.name for a in node.names if a.name == "httpx"}
            elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
                class_aliases |= {a.asname or a.name for a in node.names if a.name in CLIENT_CLASSES}
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, func: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(child, child.name if func == "<module>" else f"{func}.{child.name}")
                    continue
                if isinstance(child, ast.Call) and _is_client(child.func, module_aliases, class_aliases):
                    found.append((rel, func, child.lineno, _verdict(child)))
                visit(child, func)

        visit(tree, "<module>")
    return tuple(found)


def _is_client(func: ast.expr, module_aliases: set[str], class_aliases: set[str]) -> bool:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in module_aliases and func.attr in CLIENT_CLASSES
    return isinstance(func, ast.Name) and func.id in class_aliases


def _verdict(call: ast.Call) -> str:
    kws = {k.arg: k.value for k in call.keywords if k.arg}
    verify = kws.get("verify")
    if isinstance(verify, ast.Call):
        name = verify.func.attr if isinstance(verify.func, ast.Attribute) else getattr(verify.func, "id", "")
        if name in SHARED_CONTEXT_FUNCS:
            return "shared"
    if "transport" in kws:
        return "transport"
    return "per-client"


def test_the_scan_is_not_vacuous() -> None:
    sites = _sites()
    assert len(sites) >= MIN_SITES, f"found only {len(sites)} httpx client constructions under {SRC}"
    assert any(v == "shared" for *_x, v in sites)


def test_every_client_shares_the_ssl_context_or_is_allowed() -> None:
    offenders = [
        f"{path}:{line} in {func}()" for path, func, line, verdict in _sites()
        if verdict == "per-client" and (path, func) not in ALLOWED
    ]
    assert not offenders, (
        "httpx clients built with a per-client SSL context (each build re-reads the CA bundle from the install "
        "generation, which a long-lived process can outlive). Pass verify=get_shared_ssl_context() from "
        "nexus.db.t2.shared_ssl, or allow the site in ALLOWED with the reason its process is short-lived:\n  "
        + "\n  ".join(offenders)
    )


def test_no_allowlist_entry_is_stale() -> None:
    per_client = {(path, func) for path, func, _line, verdict in _sites() if verdict == "per-client"}
    stale = sorted(set(ALLOWED) - per_client)
    assert not stale, f"ALLOWED names sites that no longer build a per-client context: {stale}"
