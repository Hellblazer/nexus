# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o lint: how a bearer travels, read off the source.

Two rules over ``src/nexus``, ``conexus/hooks/scripts`` and ``conexus/launch``:

1. **No redirect is followed on a bearer path.** urllib copies ``Authorization`` to a redirect's
   target; httpx drops it on a cross-origin hop but follows nothing unless asked. Every urllib
   opener that carries a bearer includes ``no_redirect_handler`` (pinned by
   ``tests/db/test_bearer_redirects.py``); this lint finds every ``follow_redirects=`` set to anything
   but ``False`` and fails on one outside the allowlist, so a bearer client cannot turn following on.

2. **Every client that can carry a bearer makes an explicit proxy decision.** In a file that sets an
   ``Authorization`` header (and in the pooled opener the vector client rides on), each
   ``httpx.Client`` / ``AsyncClient`` / one-shot call that passes ``headers=``, ``build_opener`` and ``urlopen``
   must say what it does about the environment's proxy: ``trust_env=``, ``mounts=``, ``proxy=``,
   ``proxies=`` or ``transport=`` on an httpx call, a ``ProxyHandler`` among a urllib opener's handlers
   (or anywhere in its function). The default (httpx ``trust_env=True``, urllib's environment proxy)
   sends a plain-http bearer through whatever ``http_proxy`` a repository's settings name.

Both allowlists are exact in both directions (an entry that matches nothing, or a function that gained a
site, fails) and each lint carries a non-vacuity floor and a mutation table for its own detector.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (ROOT / "src" / "nexus", ROOT / "conexus" / "hooks" / "scripts", ROOT / "conexus" / "launch")

Site = tuple[str, str, str, int]  # (path, enclosing function, kind, line)

#: files that build the shared opener a bearer rides on but never spell "Authorization" themselves
ALWAYS_IN_SCOPE = frozenset({"src/nexus/db/pooled_http.py"})

HTTPX_MODULES = frozenset({"httpx", "_httpx"})
HTTPX_ONE_SHOT = frozenset({"get", "post", "put", "delete", "patch", "request", "stream", "head"})
HTTPX_CLIENTS = frozenset({"Client", "AsyncClient"})
DECISION_KEYWORDS = frozenset({"trust_env", "mounts", "proxy", "proxies", "transport"})
#: ``**bearer_httpx_kwargs(...)`` carries ``trust_env=False`` and the proxy of config.yml (nexus.db.t2.bearer_transport)
DECISION_SPLATS = frozenset({"bearer_httpx_kwargs", "_bearer_httpx_kwargs"})
#: urllib factories that decide the proxy: the stdlib handler, and the config-only handler and opener of bearer_transport / _endpoint_resolve
PROXY_HANDLER_NAMES = frozenset({"ProxyHandler", "bearer_proxy_handler", "bearer_opener"})


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _mentions(node: ast.AST, word: str) -> bool:
    return any(_name(n) == word for n in ast.walk(node))


class _Scope(ast.NodeVisitor):
    """Walks a module keeping the dotted name and the node of the enclosing function."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.functions: list[ast.AST] = []
        self.sites: list[Site] = []

    def qual(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _enter(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        if not isinstance(node, ast.ClassDef):
            self.functions.append(node)
        self.generic_visit(node)
        if not isinstance(node, ast.ClassDef):
            self.functions.pop()
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter  # noqa: N815

    def site(self, kind: str, node: ast.AST) -> None:
        self.sites.append((self.rel, self.qual(), kind, node.lineno))  # type: ignore[attr-defined]


class _Redirects(_Scope):
    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "follow_redirects" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                self.site("follow_redirects set", node)
        self.generic_visit(node)


class _Proxy(_Scope):
    def _client_kind(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Attribute):
            base = _name(func.value)
            if base in HTTPX_MODULES and func.attr in HTTPX_CLIENTS:
                return f"httpx.{func.attr}"
            if base in HTTPX_MODULES and func.attr in HTTPX_ONE_SHOT:
                # a one-shot call with no headers= cannot carry a bearer
                return f"httpx.{func.attr}" if any(kw.arg == "headers" for kw in call.keywords) else None
            if func.attr in ("build_opener", "urlopen"):
                return func.attr
        elif isinstance(func, ast.Name) and func.id == "build_opener":
            return "build_opener"
        return None

    def _decided(self, call: ast.Call, kind: str) -> bool:
        if {kw.arg for kw in call.keywords} & DECISION_KEYWORDS:
            return True
        if any(kw.arg is None and isinstance(kw.value, ast.Call) and _name(kw.value.func) in DECISION_SPLATS for kw in call.keywords):
            return True
        if kind in ("build_opener", "urlopen"):
            if any(_mentions(call, word) for word in PROXY_HANDLER_NAMES):
                return True
            # a handler list built a line above, or an opener installed before urlopen: the function decides
            return bool(self.functions) and any(_mentions(self.functions[-1], word) for word in PROXY_HANDLER_NAMES)
        return False

    def visit_Call(self, node: ast.Call) -> None:
        kind = self._client_kind(node)
        if kind is not None and not self._decided(node, kind):
            self.site(f"{kind} with no proxy decision", node)
        self.generic_visit(node)


def _sets_authorization(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and n.value == "Authorization" for n in ast.walk(tree))


def _scan(rule: type[_Scope], *, only_bearer_files: bool) -> tuple[list[Site], int, int]:
    found: list[Site] = []
    files = in_scope = 0
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            files += 1
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if only_bearer_files and not (_sets_authorization(tree) or rel in ALWAYS_IN_SCOPE):
                continue
            in_scope += 1
            walker = rule(rel)
            walker.visit(tree)
            found.extend(walker.sites)
    return found, files, in_scope


def _tally(sites: list[Site]) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for path, qual, kind, _line in sites:
        counts[(path, qual, kind)] = counts.get((path, qual, kind), 0) + 1
    return counts


def _synthetic(rule: type[_Scope], source: str, rel: str = "synthetic.py") -> list[Site]:
    walker = rule(rel)
    walker.visit(ast.parse(source))
    return walker.sites


Allowed = dict[tuple[str, str, str], tuple[int, str]]


def _allow(table: Allowed, path: str, qual: str, reason: str, kind: str, count: int = 1) -> None:
    table[(path, qual, kind)] = (count, reason)


def _check(sites: list[Site], allowed: Allowed, what: str) -> None:
    counts = _tally(sites)
    stray = {k: n for k, n in counts.items() if k not in allowed or n > allowed[k][0]}
    dead = sorted(k for k in allowed if k not in counts)
    shrunk = sorted((k, counts[k], allowed[k][0]) for k in allowed if k in counts and counts[k] < allowed[k][0])
    lines = {k: sorted(line for p, q, kind, line in sites if (p, q, kind) == k) for k in stray}
    sys.stderr.write(f"ec37o {what} lint: {len(sites)} sites in {len(counts)} groups, {len(stray)} stray groups, {len(dead)} dead entries\n")
    listing = "\n".join(f"  {p}:{lines[(p, q, k)]} in {q}: {k}" for p, q, k in lines)
    assert not stray, f"{what}: sites outside the allowlist (fix them, or add them with the reason):\n{listing}"
    assert not dead, f"{what}: allowlist entries that match no site (delete them): {dead}"
    assert not shrunk, f"{what}: allowlist entries now holding fewer sites than allowed (lower the count): {shrunk}"


# ── rule 1: no redirect is followed on a bearer path ─────────────────────────

_NO_NEXUS_BEARER = "carries no nexus bearer: a fetch of a public third-party page, file or model"
REDIRECT_ALLOWED: Allowed = {}
_AR = "src/nexus/aspect_readers.py"
_FOLLOW = "follow_redirects set"
for _fn in ("_confirm_head", "_read_https_uri", "_stat_https_uri", "capture_https_etag"):
    _allow(REDIRECT_ALLOWED, _AR, _fn, "fetches or stats a document's own https URI for an etag or its bytes; " + _NO_NEXUS_BEARER, _FOLLOW)
_allow(REDIRECT_ALLOWED, "src/nexus/commands/catalog_cmds/backfill.py", "backfill_etags_cmd", "captures etags of documents' own https URIs; " + _NO_NEXUS_BEARER, _FOLLOW)
_allow(REDIRECT_ALLOWED, "src/nexus/doctor_references.py", "run_check_references", "stats the https URIs of catalogued references; " + _NO_NEXUS_BEARER, _FOLLOW)
_allow(REDIRECT_ALLOWED, "src/nexus/db/minilm_direct.py", "_stream_archive", "downloads the public MiniLM archive, pinned by sha256; " + _NO_NEXUS_BEARER, _FOLLOW)
_allow(REDIRECT_ALLOWED, "src/nexus/db/service_bge_model.py", "_stream_once", "downloads the public bge model from a release/CDN host, pinned by sha256 (httpx drops Authorization on a cross-origin hop in any case); " + _NO_NEXUS_BEARER, _FOLLOW)


def _redirect_sites() -> tuple[list[Site], int, int]:
    return _scan(_Redirects, only_bearer_files=False)


@pytest.fixture(scope="module")
def redirect_scan() -> tuple[list[Site], int, int]:
    return _redirect_sites()


@pytest.mark.parametrize(
    ("source", "expect"),
    [
        ("import httpx\ndef f():\n    return httpx.Client(follow_redirects=True)\n", 1),
        ("import httpx\ndef f():\n    return httpx.get('u', follow_redirects=True)\n", 1),
        ("import httpx\ndef f(x):\n    return httpx.Client(follow_redirects=x)\n", 1),
        ("import httpx\ndef f():\n    return httpx.Client(follow_redirects=False)\n", 0),
        ("import httpx\ndef f():\n    return httpx.Client()\n", 0),
    ],
)
def test_the_redirect_detector_sees_follow_on_and_leaves_follow_off_alone(source: str, expect: int) -> None:
    assert len(_synthetic(_Redirects, source)) == expect


# ── rule 2: an explicit proxy decision on every client that can carry a bearer ─

_NO_DECISION = "{} with no proxy decision"
_GITHUB = "sends a GitHub token (GH_TOKEN) to GitHub's https endpoints, never a nexus credential"
_PROBE = "an unauthenticated probe: no Authorization header is built for it"
PROXY_ALLOWED: Allowed = {}
_allow(PROXY_ALLOWED, "src/nexus/daemon/binary_install.py", "_api_open", _GITHUB, _NO_DECISION.format("urlopen"), 2)
_allow(PROXY_ALLOWED, "src/nexus/daemon/binary_install.py", "_fetch_once", _GITHUB, _NO_DECISION.format("urlopen"))
_allow(PROXY_ALLOWED, "src/nexus/daemon/storage_service_daemon.py", "StorageServiceSupervisor._probe_service_health", _PROBE + " (loopback /health)", _NO_DECISION.format("urlopen"))
_allow(PROXY_ALLOWED, "src/nexus/daemon/storage_service_daemon.py", "StorageServiceSupervisor._probe_service_liveness", _PROBE + " (loopback /livez)", _NO_DECISION.format("urlopen"))
_allow(PROXY_ALLOWED, "src/nexus/health.py", "_check_cli_version", _PROBE + " (the published-version check)", _NO_DECISION.format("urlopen"))
_allow(PROXY_ALLOWED, "src/nexus/db/http_vector_client.py", "_keepalive_opener", "builds the pooled opener, whose build_opener carries the ProxyHandler decision and is itself in scope of this lint (ALWAYS_IN_SCOPE)", _NO_DECISION.format("build_opener"))

@pytest.fixture(scope="module")
def proxy_scan() -> tuple[list[Site], int, int]:
    return _scan(_Proxy, only_bearer_files=True)


@pytest.mark.parametrize(
    ("source", "expect"),
    [
        ("import httpx\ndef f():\n    return httpx.Client(timeout=1)\n", 1),
        ("import httpx\ndef f():\n    return httpx.Client(trust_env=False)\n", 0),
        ("import httpx\ndef f(m):\n    return httpx.Client(mounts=m)\n", 0),
        ("import httpx\ndef f():\n    return httpx.Client(proxy=None)\n", 0),
        ("import httpx\ndef f():\n    return httpx.Client(timeout=1, **bearer_httpx_kwargs())\n", 0),
        ("import httpx\ndef f(h, u):\n    return httpx.get(u, headers=h, **bearer_httpx_kwargs(u))\n", 0),
        ("import httpx\ndef f(h):\n    return httpx.get('u', headers=h, **other())\n", 1),
        ("import urllib.request as u\ndef f(r):\n    o = u.build_opener(bearer_proxy_handler())\n    return o.open(r)\n", 0),
        ("import httpx\ndef f():\n    return httpx.AsyncClient(transport=t)\n", 0),
        ("import httpx\ndef f():\n    return httpx.AsyncClient()\n", 1),
        ("import httpx\ndef f(h):\n    return httpx.get('u', headers=h)\n", 1),
        ("import httpx\ndef f(h):\n    return httpx.get('u', headers=h, trust_env=False)\n", 0),
        ("import httpx\ndef f():\n    return httpx.get('u', timeout=1)\n", 0),  # no headers=: cannot carry a bearer
        ("import urllib.request as u\ndef f(r):\n    return u.urlopen(r)\n", 1),
        ("import urllib.request as u\ndef f(r):\n    o = u.build_opener()\n    return o.open(r)\n", 1),
        ("import urllib.request as u\ndef f(r):\n    o = u.build_opener(u.ProxyHandler({}))\n    return o.open(r)\n", 0),
        ("import urllib.request as u\ndef f(r, x):\n    o = u.build_opener(*([] if x else [u.ProxyHandler({})]))\n    return o.open(r)\n", 0),
        ("import urllib.request as u\ndef f(r, x):\n    hs = [u.ProxyHandler({})]\n    o = u.build_opener(*hs)\n    return o.open(r)\n", 0),
        ("import urllib.request as u\ndef f(r):\n    u.install_opener(u.build_opener(u.ProxyHandler({})))\n    return u.urlopen(r)\n", 0),
    ],
)
def test_the_proxy_detector_sees_an_undecided_client_and_leaves_a_decided_one_alone(source: str, expect: int) -> None:
    assert len(_synthetic(_Proxy, source)) == expect


def test_only_a_file_that_sets_authorization_is_in_scope() -> None:
    assert _sets_authorization(ast.parse("h = {'Authorization': 'x'}"))
    assert not _sets_authorization(ast.parse("h = {'Proxy-Authorization': 'x'}"))


def test_every_follow_redirects_is_a_reviewed_fetch_of_a_public_document(redirect_scan) -> None:
    sites, _files, _in_scope = redirect_scan
    _check(sites, REDIRECT_ALLOWED, "follow_redirects")


def test_the_redirect_walk_is_not_vacuous(redirect_scan) -> None:
    sites, files, in_scope = redirect_scan
    assert files >= 500 and in_scope == files, (files, in_scope)
    assert len(sites) >= 8, f"only {len(sites)} follow_redirects sites found; the detector stopped matching"


def test_every_bearer_capable_client_makes_a_proxy_decision(proxy_scan) -> None:
    sites, _files, _in_scope = proxy_scan
    _check(sites, PROXY_ALLOWED, "proxy decision")


def test_the_proxy_walk_is_not_vacuous(proxy_scan) -> None:
    sites, files, in_scope = proxy_scan
    assert files >= 500, files
    assert in_scope >= 15, f"only {in_scope} files set an Authorization header or build the pooled opener; the scope detector stopped matching"
    # the walk finds the clients that DO decide, too: count them so a detector that only ever saw violations is a failure
    decided = _decided_clients()
    assert decided >= 12, f"only {decided} clients with an explicit proxy decision found"


def _decided_clients() -> int:
    total = 0
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if not (_sets_authorization(tree) or rel in ALWAYS_IN_SCOPE):
                continue
            walker = _Proxy(rel)
            walker.visit(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and (kind := walker._client_kind(node)) is not None:
                    # re-run the decision with no enclosing function: only the call's own keywords and arguments count
                    if walker._decided(node, kind):
                        total += 1
    return total
