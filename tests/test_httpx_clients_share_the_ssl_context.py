# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``httpx`` client in ``src/nexus`` is built on the process-wide SSL context.

``httpx.Client()``, ``httpx.AsyncClient()``, ``httpx.HTTPTransport()`` and the
module-level request helpers (``httpx.get``, ``httpx.post``, ``httpx.stream``
and the rest, each of which builds a client per call) build an SSL context
eagerly, reading the CA bundle (``certifi.where()``, inside the install
generation the process started from) at every construction. A long-lived
process (the MCP server, a daemon, the aspect worker) whose generation was
removed then fails its next build with a pathless ``[Errno 2] No such file or
directory``: on 2026-11-21/22 that broke the T1 scratch tool for the rest of
four sessions, and review of the fix found the same build in
``IdentityClient``, ``groups.py``, the engine-version probes and more.

The rule: pass ``verify=get_shared_ssl_context()`` (``nexus.db.t2.shared_ssl``),
or, for a client, a ``transport=`` that builds no SSL context (an
``httpx.MockTransport``, or a transport the caller passes in: a test seam),
or be listed in ``ALLOWED`` with the reason the process is short-lived. An
``httpx.HTTPTransport(...)`` builds its own context, so it takes ``verify=``
itself. A stale ``ALLOWED`` entry fails too, and the scan must find at least
``MIN_SITES`` constructions, so an import-shape change that hides them all
cannot pass vacuously.
"""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
#: Names on the httpx module that build an SSL context when called.
BUILDERS = frozenset({
    "Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport",
    "get", "post", "put", "patch", "delete", "head", "options", "request", "stream",
})
CLIENTS = frozenset({"Client", "AsyncClient"})
SHARED_CONTEXT_FUNCS = frozenset({"get_shared_ssl_context", "_get_shared_ssl_context"})
NO_CONTEXT_TRANSPORTS = frozenset({"MockTransport"})

#: (path under src/nexus, enclosing function) -> why a per-client SSL context is acceptable there.
ALLOWED: dict[tuple[str, str], str] = {
    ("backup/cloud_export.py", "ExportClient.__init__"): "nx backup export: a CLI command, one process per run",
    ("backup/cloud_import.py", "ImportClient.__init__"): "nx backup import: a CLI command, one process per run",
    ("commands/catalog_cmds/backfill.py", "backfill_etags_cmd"): "nx catalog backfill-etags: a CLI command",
    ("commands/doctor.py", "_run_check_mineru"): "nx doctor: a CLI command, one process per run",
    ("doctor_references.py", "run_check_references"): "nx doctor --check-references: reached only from the CLI",
}

#: The scan found 44 constructions when this was written (2026-11-23).
MIN_SITES = 35


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


@cache
def _sites() -> tuple[tuple[str, str, int, str, str], ...]:
    """``(path, enclosing function, line, builder, verdict)`` for every SSL-context-building
    httpx call; verdict is ``shared``, ``transport`` or ``per-client``."""
    found: list[tuple[str, str, int, str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_aliases = {"httpx"}
        name_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_aliases |= {a.asname or a.name for a in node.names if a.name == "httpx"}
            elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
                name_aliases |= {a.asname or a.name: a.name for a in node.names if a.name in BUILDERS}
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, func: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(child, child.name if func == "<module>" else f"{func}.{child.name}")
                    continue
                if isinstance(child, ast.Call) and (builder := _builder(child.func, module_aliases, name_aliases)):
                    found.append((rel, func, child.lineno, builder, _verdict(child, builder)))
                visit(child, func)

        visit(tree, "<module>")
    return tuple(found)


def _builder(func: ast.expr, module_aliases: set[str], name_aliases: dict[str, str]) -> str:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in module_aliases and func.attr in BUILDERS:
            return func.attr
        return ""
    if isinstance(func, ast.Name):
        return name_aliases.get(func.id, "")
    return ""


def _verdict(call: ast.Call, builder: str) -> str:
    kws = {k.arg: k.value for k in call.keywords if k.arg}
    verify = kws.get("verify")
    if isinstance(verify, ast.Call) and _call_name(verify.func) in SHARED_CONTEXT_FUNCS:
        return "shared"
    transport = kws.get("transport")
    if builder in CLIENTS and transport is not None:
        if isinstance(transport, ast.Call):
            # An HTTPTransport(...) builds its own context and is scanned as its own site.
            if _call_name(transport.func) in NO_CONTEXT_TRANSPORTS:
                return "transport"
            if _call_name(transport.func) in {"HTTPTransport", "AsyncHTTPTransport"}:
                return "transport"
            return "per-client"
        return "transport"  # passed in by the caller: a test seam
    return "per-client"


def test_the_scan_is_not_vacuous() -> None:
    sites = _sites()
    assert len(sites) >= MIN_SITES, f"found only {len(sites)} httpx SSL-context builds under {SRC}"
    builders = {b for *_x, b, _v in sites}
    assert {"Client", "get"} <= builders, builders
    assert any(v == "shared" for *_x, v in sites)


def test_every_build_shares_the_ssl_context_or_is_allowed() -> None:
    offenders = [
        f"{path}:{line} httpx.{builder}() in {func}" for path, func, line, builder, verdict in _sites()
        if verdict == "per-client" and (path, func) not in ALLOWED
    ]
    assert not offenders, (
        "httpx builds with a per-call SSL context (each re-reads the CA bundle from the install generation, which "
        "a long-lived process can outlive). Pass verify=get_shared_ssl_context() from nexus.db.t2.shared_ssl, "
        "or allow the site in ALLOWED with the reason its process is short-lived:\n  " + "\n  ".join(offenders)
    )


def test_no_allowlist_entry_is_stale() -> None:
    per_client = {(path, func) for path, func, _line, _b, verdict in _sites() if verdict == "per-client"}
    stale = sorted(set(ALLOWED) - per_client)
    assert not stale, f"ALLOWED names sites that no longer build a per-call context: {stale}"


@pytest.mark.parametrize(("source", "verdict"), [
    ("import httpx\nhttpx.Client(transport=httpx.MockTransport(h))\n", "transport"),
    ("import httpx\ndef f(t):\n    httpx.Client(transport=t)\n", "transport"),
    ("import httpx\nhttpx.Client(transport=SomethingElse())\n", "per-client"),
    ("import httpx\nhttpx.HTTPTransport(retries=2)\n", "per-client"),
    ("import httpx\nhttpx.HTTPTransport(verify=get_shared_ssl_context())\n", "shared"),
    ("import httpx\nhttpx.get(u, timeout=2)\n", "per-client"),
    ("import httpx as h\nh.stream('GET', u, verify=get_shared_ssl_context())\n", "shared"),
    ("from httpx import post\npost(u)\n", "per-client"),
], ids=["mock-transport", "seam-transport", "unknown-transport", "http-transport", "http-transport-shared",
        "module-get", "aliased-stream-shared", "from-import-post"])
def test_the_verdicts(source: str, verdict: str) -> None:
    tree = ast.parse(source)
    module_aliases = {"httpx"} | {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                  for a in n.names if a.name == "httpx"}
    name_aliases = {a.asname or a.name: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                    for a in n.names if a.name in BUILDERS}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _builder(n.func, module_aliases, name_aliases)]
    outer = min(calls, key=lambda c: (c.lineno, c.col_offset))
    assert _verdict(outer, _builder(outer.func, module_aliases, name_aliases)) == verdict
