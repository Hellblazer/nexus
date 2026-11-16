# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 Step 1 (nexus-rfvyy.6.2): the LLM-surface registry lint.

Every place ``src/nexus`` can send text to a model or off the machine must sit
in a function that a row of ``src/nexus/tables/llm-surfaces.toml`` names as
``module:function``. The sites are found by an AST scan:

* a call to ``claude_dispatch`` or ``run_text_sync`` (by name, by an
  ``import ... as`` alias, as an attribute such as ``dispatch.claude_dispatch``,
  through ``getattr(x, "claude_dispatch")``), and a bare reference to either
  that is not a call (``run_in_executor(None, run_text_sync, ...)``);
* an argv list or tuple that builds ``claude -p``: an element naming claude
  (the string ``"claude"``, or a variable whose name contains ``claude``)
  directly followed by the string ``"-p"``. ``psql -p`` and port flags have
  another program before them and are not matched;
* an ``httpx`` request or client construction (``httpx.get/post/...``,
  ``httpx.Client/AsyncClient/HTTPTransport``, through ``import httpx as x`` or
  ``from httpx import ...``) outside the engine-client modules below.

A site is registered when the registry has a row whose target names its
enclosing function, or an enclosing class or function of it (a row naming
``mod:Class.method`` covers the nested helpers inside that method).

The registry landed at nexus-rfvyy.6.16 (``src/nexus/tables/llm-surfaces.toml``, typed
loader ``nexus.tables.llm_surfaces``). A row names several targets and every one counts as
registered. The scan does not depend on the table, so its counts are real and
``MIN_SITES_EXAMINED`` is the count at landing. First run with the table: 61 sites examined
(claude-argv=2, dispatch=21, httpx=38), 17 engine-client (allowlisted), 44 in scope, 0 unregistered.

Blind spots, stated so nobody reads a green run as more than it is:

* a call made through a name the scan cannot see (a function stored in a dict
  and looked up, ``functools.partial`` of the target, a re-export under another
  module's alias that is itself imported with ``import *``);
* an argv assembled by concatenation or ``list.append`` (``["claude"] + flags``)
  rather than written as one literal;
* a request made with a client object built elsewhere and passed in (the
  construction site is the one found, and is registered or allowlisted);
* outbound traffic through ``urllib``, ``http.client`` or a library that owns
  its transport (``voyageai``): outside this lint's mandate (RDR-243 names
  ``httpx``); the engine's own egress is pinned Java-side by
  ``EgressGuardCensus``.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from tests._llm_surfaces import REPO_ROOT, load_surfaces, registry_missing_message

pytestmark = pytest.mark.lint

SRC = REPO_ROOT / "src"

#: The non-vacuity floor: the number of sites the scan examined at landing
#: (2026-11-04, develop f2dc0b78c). The scan must find at least this many; a
#: refactor that makes the AST walk stop seeing sites fails here instead of
#: passing over nothing. Raise it when sites are added; lower it only with the
#: reason in the commit.
MIN_SITES_EXAMINED = 61  # first live run on develop f2dc0b78c: claude-argv=2, dispatch=21, httpx=38 (RDR-243 census: 21 dispatch + 2 argv)

DISPATCH_NAMES: frozenset[str] = frozenset({"claude_dispatch", "run_text_sync"})
HTTPX_REQUEST_ATTRS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send"}
)
HTTPX_CLIENT_ATTRS: frozenset[str] = frozenset({"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport"})
HTTPX_ATTRS: frozenset[str] = HTTPX_REQUEST_ATTRS | HTTPX_CLIENT_ATTRS

#: Modules whose httpx clients talk to the nexus engine (the local service or the
#: managed tenant) or to an in-process fake of it. RDR-243: "outside the engine-client
#: modules". These carry store traffic, not LLM or third-party traffic. Dotted module names.
ENGINE_CLIENT_MODULES: frozenset[str] = frozenset({
    "nexus.backup.cloud_export",         # the tenant export session routes (nx backup, RDR-248)
    "nexus.backup.cloud_import",         # the tenant import session routes (nx backup restore, RDR-248)
    "nexus.db.http_engine_status",       # GET /v1/status
    "nexus.db.http_scratch_store",       # T1 over the engine
    "nexus.db.inmemory_pipeline",        # httpx.MockTransport onto an in-process fake engine
    "nexus.db.managed_endpoint",         # GET /version on the managed service
    "nexus.db.serving_engine_version",   # GET /version on the serving engine
    "nexus.db.t2._refreshable_client",   # the shared T2 client
    "nexus.db.t2.http_centroid_store",
    "nexus.db.t2.http_token_store",
    "nexus.daemon.storage_service_daemon",    # the supervisor's loopback admin post
    "nexus.upgrade_ladder.provisioning",      # engine version, capability and health probes
})

#: Engine probes that live in modules which also hold non-engine requests, so the
#: module cannot be allowlisted whole. ``module:qualname``. nexus-rfvyy.6.16 may move
#: these into the registry as endpoint ``none`` rows instead; nothing else belongs here.
ENGINE_CLIENT_FUNCTIONS: frozenset[str] = frozenset({
    "nexus.commands.doctor:_run_trim_telemetry",          # GET {engine}/version
    "nexus.health:_check_static_service_token",           # GET {engine}/v1/_whoami
    "nexus.health:_check_storage_service_health",         # GET {engine} health url
    "nexus.health:probe_t2_schema_fingerprint",           # GET {engine}/version
})


@dataclass(frozen=True)
class Site:
    kind: str       # dispatch | dispatch-ref | dispatch-getattr | claude-argv | httpx
    module: str     # dotted module name
    qualname: str   # enclosing Class.func.inner chain, or "<module>"
    line: int
    detail: str

    @property
    def where(self) -> str:
        return f"{self.module}:{self.qualname}"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _scopes(tree: ast.AST) -> dict[int, str]:
    """id(node) -> dotted chain of enclosing classes and functions."""
    out: dict[int, str] = {}

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = ".".join(stack) or "<module>"
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*stack, child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return out


def _is_claude_name(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lower().endswith("claude")
    if isinstance(node, ast.Name):
        return "claude" in node.id.lower()
    if isinstance(node, ast.Attribute):
        return "claude" in node.attr.lower()
    return False


def scan_source(source: str, module: str) -> list[Site]:
    """Every site in one module's source (the scan's whole logic, testable on a snippet)."""
    tree = ast.parse(source)
    scope = _scopes(tree)
    sites: list[Site] = []

    dispatch_names = set(DISPATCH_NAMES)
    httpx_mods: set[str] = set()
    httpx_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in DISPATCH_NAMES:
                    dispatch_names.add(alias.asname or alias.name)
                if node.module == "httpx" and alias.name in HTTPX_ATTRS:
                    httpx_names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    httpx_mods.add(alias.asname or "httpx")

    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    def add(kind: str, node: ast.AST, detail: str) -> None:
        sites.append(Site(kind, module, scope.get(id(node), "<module>"), node.lineno, detail))  # type: ignore[attr-defined]

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in dispatch_names:
                add("dispatch", node, f.id)
            elif isinstance(f, ast.Attribute) and f.attr in dispatch_names:
                add("dispatch", node, f.attr)
            elif (
                isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in DISPATCH_NAMES
            ):
                add("dispatch-getattr", node, str(node.args[1].value))
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in httpx_mods and f.attr in HTTPX_ATTRS:
                add("httpx", node, f"httpx.{f.attr}")
            elif isinstance(f, ast.Name) and f.id in httpx_names:
                add("httpx", node, f"httpx.{httpx_names[f.id]}")
        elif isinstance(node, (ast.List, ast.Tuple)):
            for first, second in zip(node.elts, node.elts[1:]):
                if _is_claude_name(first) and isinstance(second, ast.Constant) and second.value == "-p":
                    add("claude-argv", node, "claude -p argv")
                    break
        elif (
            isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            and node.id in dispatch_names and id(node) not in call_funcs
        ):
            add("dispatch-ref", node, node.id)
        elif (
            isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
            and node.attr in DISPATCH_NAMES and id(node) not in call_funcs
        ):
            add("dispatch-ref", node, node.attr)
    return sites


@cache
def scan_repo() -> tuple[tuple[Site, ...], int]:
    """(sites, files scanned) over ``src/nexus/**/*.py``."""
    sites: list[Site] = []
    files = sorted((SRC / "nexus").rglob("*.py"))
    for path in files:
        sites.extend(scan_source(path.read_text(encoding="utf-8"), _module_name(path)))
    return tuple(sorted(sites, key=lambda s: (s.module, s.line))), len(files)


def is_engine_client(site: Site) -> bool:
    if site.kind != "httpx":
        return False
    if site.module in ENGINE_CLIENT_MODULES:
        return True
    return any(site.where == f or site.where.startswith(f + ".") for f in ENGINE_CLIENT_FUNCTIONS)


def _prefixes(qualname: str) -> list[str]:
    parts = qualname.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def registered_targets(targets: set[str], site: Site) -> bool:
    """A row names the site's function, or a class or function enclosing it."""
    if site.qualname == "<module>":
        return f"{site.module}:<module>" in targets
    return any(f"{site.module}:{p}" in targets for p in _prefixes(site.qualname))


def _describe(sites: list[Site], limit: int = 80) -> str:
    lines = [f"  {s.kind:16} {s.where}  (line {s.line}, {s.detail})" for s in sites[:limit]]
    if len(sites) > limit:
        lines.append(f"  ... and {len(sites) - limit} more")
    return "\n".join(lines)


def _counts(sites: tuple[Site, ...] | list[Site]) -> str:
    by_kind: dict[str, int] = {}
    for s in sites:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))


# --------------------------------------------------------------------------- #
# The lint.
# --------------------------------------------------------------------------- #


def test_every_llm_and_outbound_site_sits_in_a_function_the_registry_names() -> None:
    sites, n_files = scan_repo()
    in_scope = [s for s in sites if not is_engine_client(s)]
    problems: list[str] = []

    if len(sites) < MIN_SITES_EXAMINED:
        problems.append(
            f"the scan examined {len(sites)} site(s), below the landing floor {MIN_SITES_EXAMINED}: "
            "the walk stopped seeing sites (or sites were removed; lower the floor with the reason)"
        )

    surfaces = load_surfaces()
    if surfaces is None:
        problems.append(registry_missing_message())
        unregistered = in_scope
    else:
        targets = {t for s in surfaces for t in s.targets}
        unregistered = [s for s in in_scope if not registered_targets(targets, s)]

    summary = (
        f"examined {len(sites)} site(s) in {n_files} file(s) ({_counts(sites)}); "
        f"{len(sites) - len(in_scope)} engine-client (allowlisted); {len(in_scope)} need a registry row; "
        f"{len(unregistered)} unregistered; floor MIN_SITES_EXAMINED={MIN_SITES_EXAMINED}"
    )
    if unregistered:
        problems.append(
            f"{len(unregistered)} site(s) sit in a function no registry row names "
            "(add a row with the function as `module:function`, or, for an engine client, the allowlist here):\n"
            + _describe(unregistered)
        )
    assert not problems, summary + "\n" + "\n".join(problems)


def test_registry_targets_name_functions_that_exist() -> None:
    """A row whose ``module:function`` no longer exists is stale: it would keep
    'covering' a site that moved. Every target resolves to a real scope."""
    surfaces = load_surfaces()
    assert surfaces is not None, registry_missing_message()
    assert surfaces, "the registry holds no rows"
    missing: list[str] = []
    for s, target in ((s, t) for s in surfaces for t in s.targets):
        module, _, qual = target.partition(":")
        path = SRC / Path(*module.split("."))
        file = path.with_suffix(".py") if path.with_suffix(".py").is_file() else path / "__init__.py"
        if not file.is_file():
            missing.append(f"{s.name}: module {module} has no source file")
            continue
        if qual == "<module>":
            continue
        scopes = {v for v in _scopes(ast.parse(file.read_text(encoding="utf-8"))).values()}
        if qual not in scopes:
            missing.append(f"{s.name}: {target} names no function or class in {file.relative_to(REPO_ROOT)}")
    assert not missing, "stale registry rows:\n  " + "\n  ".join(missing)


# --------------------------------------------------------------------------- #
# The scan itself, pinned on snippets and on the repo, so the lint cannot pass
# by looking at nothing. None of these needs the registry.
# --------------------------------------------------------------------------- #


def test_scan_finds_the_dispatch_forms_including_aliases_getattr_and_references() -> None:
    src = (
        "from nexus.operators.dispatch import claude_dispatch as cd\n"
        "from nexus.operators import dispatch\n"
        "import nexus.operators.llm_backend as lb\n"
        "async def a(): return await cd('p', {})\n"
        "def b(): return dispatch.claude_dispatch('p', {})\n"
        "def c(): return lb.run_text_sync('x', 'p', model=None, timeout=1)\n"
        "def d(m): return getattr(m, 'claude_dispatch')('p', {})\n"
        "def e(loop): return loop.run_in_executor(None, run_text_sync, 'x')\n"
        "class K:\n"
        "    def m(self):\n"
        "        def inner(): return claude_dispatch('p', {})\n"
        "        return inner\n"
        "def not_a_site(): return 'claude_dispatch'\n"
    )
    found = {(s.kind, s.qualname) for s in scan_source(src, "nexus.x")}
    assert found == {
        ("dispatch", "a"), ("dispatch", "b"), ("dispatch", "c"), ("dispatch-getattr", "d"),
        ("dispatch-ref", "e"), ("dispatch", "K.m.inner"),
    }, found


def test_scan_finds_claude_argv_and_not_psql_port_flags() -> None:
    src = (
        "def spawn(claude_path):\n"
        "    return ['claude', '-p', '--output-format', 'json'], (claude_path, '-p'), ['psql', '-p', '5432']\n"
        "def other():\n"
        "    return ['pg_ctl', '-p', 'x'], ['claude', '--version'], ['not-claude-code', '-p']\n"
    )
    found = [(s.kind, s.qualname) for s in scan_source(src, "nexus.x")]
    assert found.count(("claude-argv", "spawn")) == 2
    assert [f for f in found if f[1] == "other"] == [], (
        "pg_ctl -p, `claude --version` and a program merely containing 'claude-' are not `claude -p`"
    )


def test_scan_finds_httpx_through_every_import_spelling() -> None:
    src = (
        "import httpx\nimport httpx as hx\nfrom httpx import post as p, Client\n"
        "def a(): return httpx.get('u')\n"
        "def b(): return hx.AsyncClient()\n"
        "def c(): return p('u')\n"
        "def d(): return Client()\n"
        "def e(): return httpx.HTTPError, httpx.Timeout(3)\n"
    )
    found = sorted((s.qualname, s.detail) for s in scan_source(src, "nexus.x"))
    assert found == [("a", "httpx.get"), ("b", "httpx.AsyncClient"), ("c", "httpx.post"), ("d", "httpx.Client")]


def test_registered_targets_cover_nested_helpers_but_not_siblings() -> None:
    nested = Site("httpx", "nexus.m", "Cls.method.inner", 1, "httpx.get")
    sibling = Site("httpx", "nexus.m", "Cls.other", 1, "httpx.get")
    top = Site("httpx", "nexus.m", "<module>", 1, "httpx.get")
    assert registered_targets({"nexus.m:Cls.method"}, nested)
    assert registered_targets({"nexus.m:Cls"}, nested)
    assert not registered_targets({"nexus.m:Cls.method"}, sibling)
    assert not registered_targets({"nexus.m:method"}, nested)
    assert registered_targets({"nexus.m:<module>"}, top)


def test_the_repo_scan_sees_the_known_homes_and_meets_the_floor() -> None:
    sites, n_files = scan_repo()
    homes = {(s.kind, s.where) for s in sites}
    # One known site per kind, so a walk that stops seeing a whole kind fails.
    assert any(k == "claude-argv" and w.startswith("nexus.operators.dispatch:") for k, w in homes)
    assert any(k == "claude-argv" and w.startswith("nexus.aspect_extractor:") for k, w in homes)
    assert any(k == "dispatch" and w.startswith("nexus.mcp.core:") for k, w in homes)
    assert any(k == "dispatch" and w.startswith("nexus.commands.taxonomy_cmd:") for k, w in homes)
    assert any(k == "dispatch" and w.startswith("nexus.aspect_extractor:") for k, w in homes), "run_text_sync"
    assert any(k == "httpx" and w.startswith("nexus.pdf_extractor:") for k, w in homes)
    assert any(k == "httpx" and w.startswith("nexus.operators.llm_api:") for k, w in homes)
    assert n_files > 200, f"the walk saw only {n_files} source files under src/nexus"
    assert len(sites) >= MIN_SITES_EXAMINED


def test_the_engine_client_allowlist_has_no_stale_entries() -> None:
    sites, _ = scan_repo()
    http_modules = {s.module for s in sites if s.kind == "httpx"}
    http_wheres = {s.where for s in sites if s.kind == "httpx"}
    stale_modules = sorted(ENGINE_CLIENT_MODULES - http_modules)
    stale_functions = sorted(
        f for f in ENGINE_CLIENT_FUNCTIONS if not any(w == f or w.startswith(f + ".") for w in http_wheres)
    )
    assert not stale_modules and not stale_functions, (
        f"allowlist entries that no longer hold an httpx site: modules {stale_modules}, functions {stale_functions}"
    )
