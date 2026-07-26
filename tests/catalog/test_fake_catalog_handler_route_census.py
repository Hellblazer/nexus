# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0948s: mechanized drift guard, FakeCatalogHandler vs the real Java.

Both nexus-8y1tm reviewers (2026-07-04, critique record: T2
``nexus/critique-8y1tm-shape-parity-tripwire-2026-07-04.md``, finding #2)
flagged that ``test_shape_parity_tripwire.py``'s whole harness rests on an
unmechanized assumption: FakeCatalogHandler's hand-written JSON
(``tests/catalog/test_http_catalog_client.py``) mirrors the real
``CatalogHandler.java`` route switch and ``CatalogRepository.java`` row
builders. Nothing caught the fake ROTTING when the Java side changed — the
same silent-rot class the 8y1tm tripwire fixed for the Catalog/
HttpCatalogClient *method* surface, one layer down at the wire *route* and
*field* level. The route surface already grew 13+ routes across the
8y1tm/u26b4/gc2ze arc with zero automated safeguard before this guard existed.

Two independent checks, both pure source-text/AST parsing (no Java
toolchain, no compilation, no running engine, no Docker):

1. ``test_every_catalog_handler_route_is_faked_or_excluded`` — a route
   *census*: every ``case "/x" -> handleX(...)`` in CatalogHandler.java's
   dispatch switch must have either a matching ``elif op == "/x":`` branch in
   FakeCatalogHandler, or a documented ``ROUTE_EXCLUSIONS`` entry. Mirrors the
   REGISTRY/EXCLUSIONS completeness-gate pattern
   ``test_shape_parity_tripwire.py`` already established for the Python
   method surface (``test_every_shared_method_is_registered_or_excluded``).
   Checked in BOTH directions: a route added to Java with no fake coverage,
   and a fake route whose Java counterpart was removed/renamed (dead code).

2. ``test_field_shape_parity_for_hand_verified_methods`` — for the 3
   CatalogRepository.java row-builder methods the 2026-07-04 critique
   manually cross-referenced against the fake and found accurate that day
   (``stats()``, ``collRow()``, ``orphanedDocs()``), mechanizes that manual
   check: extract the JSON key set each Java method actually ``.put()``s,
   extract the JSON key set the corresponding FakeCatalogHandler route
   actually sends, assert equal. A hand-verification is only as good as the
   day it was done; this makes the same verification re-run on every CI run.

WHY NOT the bead's other candidates:

- A source-hash tripwire (candidate 1, the bead's own wording: "noisy") fires
  on ANY edit to CatalogHandler.java/CatalogRepository.java — comment
  changes, unrelated method reordering, log-message tweaks — none of which
  touch a wire shape. A guard that cries wolf gets blessed reflexively
  (`~/.claude/CLAUDE.md` "no blanket test reruns" / this repo's own
  documented failure mode) and then stops catching the ONE edit that
  matters. This guard instead diffs STRUCTURE (route labels, JSON keys), so
  it is silent on a docstring tweak and loud, with the exact route/key names,
  on an actual shape change.
- Golden fixtures generated from the Java tests (candidate 3) requires
  running the Java toolchain (Maven/JUnit) to regenerate the fixtures — that
  violates this bead's "no Java toolchain, no compilation" constraint for the
  guard itself, and worse, makes the guard's own freshness dependent on
  someone remembering to run a SEPARATE, unmechanized regeneration step
  before it can be trusted — reintroducing exactly the manual-parity problem
  this bead exists to close.
- A route/shape checksum (candidate 2, taken literally as an opaque hash)
  tells a developer only THAT something changed, not WHAT — they would still
  have to diff CatalogHandler.java against FakeCatalogHandler by hand to find
  it, which is the status quo this bead is meant to improve on. This guard
  keeps candidate 2's "per-endpoint" granularity but replaces the hash with
  an explicit structural diff (missing/extra route or key names), so the
  failure message alone answers "what changed" and "how to fix" — see the
  REQUIREMENTS in nexus-0948s: a developer must be able to tell in under a
  minute what changed, whether it matters, and how to fix or bless it.

EXISTING DRIFT FOUND while building this guard (reported, not silently
fixed — out of this bead's file-touch scope): 9 CatalogHandler.java routes
have no FakeCatalogHandler branch at all and silently fall through to its
generic catchalls (`{"error": "unknown GET op"}` 404 for GET, or
`{"ok": True}` 200 for POST) — see ``ROUTE_EXCLUSIONS`` below for the
per-route reason. Of these, ``/doc/register_many`` is the most interesting:
it IS on the Catalog/HttpCatalogClient shared surface and DOES have a
``test_shape_parity_tripwire.py`` REGISTRY entry that calls the live fake
server — but ``HttpCatalogClient.register_many``'s page-failure fallback
(nexus-9dvqy) treats the fake's shapeless ``{"ok": True}`` catchall as a
failed page and silently falls back to per-doc ``/doc/register`` (which IS
faked), so the parity test passes green while never actually exercising
``/doc/register_many``'s real wire shape. That masking gap is real and
pre-existing; it is documented here, not fixed here.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JAVA_HANDLER = (
    _REPO_ROOT / "service/src/main/java/dev/nexus/service/http/CatalogHandler.java"
)
_JAVA_REPOSITORY = (
    _REPO_ROOT / "service/src/main/java/dev/nexus/service/db/CatalogRepository.java"
)
_FAKE_HANDLER = _REPO_ROOT / "tests/catalog/test_http_catalog_client.py"

_CASE_ROUTE_RE = re.compile(r'case\s+"(/[A-Za-z0-9_/-]+)"\s*->')
_FAKE_OP_RE = re.compile(r'op\s*==\s*"(/[A-Za-z0-9_/-]+)"')
_PUT_KEY_RE = re.compile(r'\.put\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')


# ── file I/O ──────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(
            f"{path} does not exist — CatalogHandler.java/CatalogRepository.java/"
            f"test_http_catalog_client.py must have moved; update the path "
            f"constants in test_fake_catalog_handler_route_census.py (nexus-0948s)"
        )
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AssertionError(f"{path} is empty — census would be vacuous")
    return text


# ── 1. route census (Java case labels vs fake op labels) ───────────────────


def _extract_java_routes(source: str) -> set[str]:
    routes = set(_CASE_ROUTE_RE.findall(source))
    if not routes:
        # Empty-census trap (nexus-0948s REQUIREMENTS): zero routes parsed is
        # a hard failure, never a silent pass — it means the regex no longer
        # matches CatalogHandler.java's dispatch style (rewritten to a
        # different construct) and this guard has gone blind.
        raise AssertionError(
            "parsed ZERO routes from CatalogHandler.java's switch statement "
            "— either the file moved/was rewritten to a different dispatch "
            "style, or the case-label regex (`case \"/...\" ->`) no longer "
            "matches. A guard that silently found nothing to compare is "
            "worse than no guard at all (nexus-0948s empty-census trap)."
        )
    return routes


def _extract_fake_routes(source: str) -> set[str]:
    # Scope to the FakeCatalogHandler class body (do_GET + do_POST) so an
    # unrelated "op ==" string elsewhere in this 1700+-line test file can't
    # leak into the census.
    start_marker = "class FakeCatalogHandler"
    end_marker = "\ndef start_fake_server("
    if start_marker not in source or end_marker not in source:
        raise AssertionError(
            f"could not locate {start_marker!r}..{end_marker!r} in "
            f"{_FAKE_HANDLER} — FakeCatalogHandler was renamed or moved; "
            f"update the markers in test_fake_catalog_handler_route_census.py"
        )
    body = source[source.index(start_marker) : source.index(end_marker)]
    routes = set(_FAKE_OP_RE.findall(body))
    if not routes:
        raise AssertionError(
            "parsed ZERO routes from FakeCatalogHandler's do_GET/do_POST — "
            "either the class moved/was rewritten to a different dispatch "
            "style, or the `op == \"/...\"` pattern no longer matches. Empty "
            "census = vacuous pass, which this guard treats as a hard "
            "failure (nexus-0948s empty-census trap)."
        )
    return routes


#: Java routes with NO FakeCatalogHandler branch — each falls through to the
#: fake's generic catchall (`{"error": "unknown GET op"}` 404 for GET,
#: `{"ok": True}` 200 for POST). Auditable, never silent — the same
#: discipline as test_shape_parity_tripwire.py's EXCLUSIONS dict. Every entry
#: here is drift *found*, not drift *fixed* (out of this bead's file-touch
#: scope); if a future change makes one of these routes reachable through the
#: live fake server, delete the entry (the completeness gate below refuses a
#: stale one).
ROUTE_EXCLUSIONS: dict[str, str] = {
    "/collections/delete": (
        "backs HttpCatalogClient.delete_collection() (RDR-164 P2) — NOT "
        "Catalog.delete_collection_projection (different name/shape by "
        "design), so it is outside the Catalog/HttpCatalogClient "
        "shared-surface parity gate in test_shape_parity_tripwire.py "
        "entirely; delete_collection() is unit-tested against a mocked "
        "_post, not the live fake server"
    ),
    "/update_many": (
        "HttpCatalogClient-only batch optimization (nexus-xedhp) with no "
        "Catalog equivalent method, so it is outside the shared-surface "
        "parity gate; update_many() is unit-tested against a mocked _post "
        "(test_update_many_pages_and_preserves_order et al.), not the live "
        "fake server"
    ),
    "/delete_many": (
        "HttpCatalogClient-only batch optimization (nexus-xedhp) with no "
        "Catalog equivalent method, so it is outside the shared-surface "
        "parity gate; delete_many() is unit-tested against a mocked _post, "
        "not the live fake server"
    ),
    "/doc/register_many": (
        "IS on the shared surface (register_many exists on both Catalog and "
        "HttpCatalogClient) and DOES have a live-fake-server REGISTRY entry "
        "in test_shape_parity_tripwire.py — but the fake's generic "
        "`else: send({'ok': True})` catchall lacks a 'tumblers' key, so "
        "HttpCatalogClient.register_many's page-failure fallback "
        "(nexus-9dvqy) silently treats the page as failed and retries "
        "per-doc via /doc/register (which IS faked), masking the gap. The "
        "batch route's real wire shape is validated by the Java-side tests, "
        "not this harness — a pre-existing masking gap found while building "
        "this guard (nexus-0948s), documented here rather than fixed, per "
        "this bead's file-touch constraints"
    ),
    "/manifest/write_many": (
        "backs HttpCatalogClient's manifest write-many batch path via raw "
        "_post — unit-tested against a mocked _post, not the live fake "
        "server"
    ),
    "/manifest/resync": (
        "backs HttpCatalogClient.resync_chunk_count_cache() via raw _post "
        "— unit-tested against a mocked _post, not the live fake server"
    ),
    "/links/orphaned": (
        "no Python caller exists yet (grep across src/ and tests/ finds "
        "none); added server-side ahead of client wiring (nexus-ysrwi: "
        "\"...which is why the client could not build a doctor check\"). "
        "When a client method lands, it needs either a REGISTRY parity "
        "entry (if it joins the Catalog/HttpCatalogClient shared surface) "
        "or a FakeCatalogHandler branch, whichever comes first — this "
        "exclusion must be removed at that point"
    ),
    "/import/chunk": (
        "ETL bulk-import path called via raw client._post() from "
        "src/nexus/db/t2/catalog_etl.py, not through a named "
        "HttpCatalogClient method — outside the Catalog/HttpCatalogClient "
        "shared-surface parity gate by construction; exercised by the "
        "RDR-159/RDR-176 migration test suites, not this harness"
    ),
    "/import/collection": (
        "ETL bulk-import path called via raw client._post() from "
        "src/nexus/db/t2/catalog_etl.py, not through a named "
        "HttpCatalogClient method — outside the Catalog/HttpCatalogClient "
        "shared-surface parity gate by construction; exercised by the "
        "RDR-159/RDR-176 migration test suites, not this harness"
    ),
}

#: Fake routes with NO CatalogHandler.java counterpart — either they belong
#: to a different HTTP handler class entirely, or the reason must say so.
KNOWN_NON_CATALOG_HANDLER_FAKE_ROUTES: dict[str, str] = {
    "/v1/chash/lookup": (
        "belongs to ChashHandler.java (service/src/main/java/dev/nexus/"
        "service/http/ChashHandler.java), a different HTTP handler class "
        "registered on a different top-level path — not part of "
        "CatalogHandler.java's route switch, so it can never appear there"
    ),
}


def test_every_catalog_handler_route_is_faked_or_excluded() -> None:
    """The mechanized gate: every CatalogHandler.java route is either faked
    or excluded-with-reason, in BOTH directions.

    A route ADDED to CatalogHandler.java's switch with no matching
    FakeCatalogHandler branch and no ROUTE_EXCLUSIONS entry fails here —
    this is what makes it impossible to ship a new Java route that silently
    falls through the fake's generic catchall in every test that exercises
    it via the live fake server.

    A route REMOVED/renamed in CatalogHandler.java that still has a
    FakeCatalogHandler branch (dead fake code hand-authored against a route
    that no longer exists) ALSO fails here — the "extra" direction, since a
    fake wire shape asserting something the real service no longer serves is
    exactly the same rot in the opposite direction.
    """
    java_routes = _extract_java_routes(_read(_JAVA_HANDLER))
    fake_routes = _extract_fake_routes(_read(_FAKE_HANDLER))

    excluded = set(ROUTE_EXCLUSIONS)
    known_foreign = set(KNOWN_NON_CATALOG_HANDLER_FAKE_ROUTES)

    lazy = {k: v for k, v in ROUTE_EXCLUSIONS.items() if len(v.strip()) < 20}
    assert not lazy, (
        f"ROUTE_EXCLUSIONS entries with trivial reasons (gate-gaming guard): "
        f"{lazy}"
    )

    overlap = fake_routes & excluded
    assert not overlap, (
        f"routes both faked AND excluded — the exclusion is stale, the fake "
        f"branch now exists, remove the ROUTE_EXCLUSIONS entry: "
        f"{sorted(overlap)}"
    )

    stale_exclusions = excluded - java_routes
    assert not stale_exclusions, (
        f"ROUTE_EXCLUSIONS references routes no longer in CatalogHandler.java's "
        f"switch (removed or renamed) — delete the stale entry: "
        f"{sorted(stale_exclusions)}"
    )

    # Direction 1: every real Java route is covered (faked or excluded).
    uncovered = java_routes - fake_routes - excluded
    assert not uncovered, (
        f"{len(uncovered)} CatalogHandler.java route(s) have neither a "
        f"matching FakeCatalogHandler branch nor a ROUTE_EXCLUSIONS entry: "
        f"{sorted(uncovered)}\n"
        f"WHAT changed: a new `case \"/...\" -> handleX(...)` was added to "
        f"CatalogHandler.java's dispatch switch.\n"
        f"WHETHER IT MATTERS: it matters for any test that exercises this "
        f"route through the live fake server (start_fake_server()) — right "
        f"now an unmatched op silently returns FakeCatalogHandler's generic "
        f"catchall ({{'error': ...}} 404 for GET, {{'ok': True}} 200 for "
        f"POST), which either fails loudly (safe-ish) or masks a real shape "
        f"mismatch behind a fake success (unsafe — see the /doc/register_many "
        f"masking gap documented in ROUTE_EXCLUSIONS above).\n"
        f"HOW TO FIX: add an `elif op == \"/newroute\":` branch to "
        f"FakeCatalogHandler mirroring CatalogHandler.java's real response "
        f"shape (preferred — then also add a REGISTRY parity entry to "
        f"test_shape_parity_tripwire.py if the route backs a shared "
        f"Catalog/HttpCatalogClient method), OR add a ROUTE_EXCLUSIONS entry "
        f"above with a specific reason if the route is genuinely out of this "
        f"harness's scope."
    )

    # Direction 2: every fake route maps to a real Java route (or a known
    # foreign handler) — catches dead fake branches for removed/renamed routes.
    phantom = fake_routes - java_routes - known_foreign
    assert not phantom, (
        f"FakeCatalogHandler implements route(s) with no CatalogHandler.java "
        f"counterpart: {sorted(phantom)}\n"
        f"WHAT changed: either the Java route was removed/renamed (this fake "
        f"branch is dead code asserting a shape the real service no longer "
        f"serves), or it genuinely belongs to a different handler class.\n"
        f"HOW TO FIX: delete the stale FakeCatalogHandler branch if the Java "
        f"route is gone, OR add it to KNOWN_NON_CATALOG_HANDLER_FAKE_ROUTES "
        f"above with a reason naming the real handler class (mirroring "
        f"/v1/chash/lookup -> ChashHandler.java)."
    )


# ── 2. field-shape census (Java .put() keys vs fake JSON keys) ─────────────


def _find_method_body_start(source: str, method_name: str) -> int:
    pattern = re.compile(
        rf"(?:private|public|protected)[^\n{{;]*\b{re.escape(method_name)}\s*\("
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(
            f"could not find a method signature for {method_name}() in "
            f"CatalogRepository.java — it was renamed, deleted, or moved. "
            f"If intentional, update the FIELD_SHAPE_CHECKS method name in "
            f"test_fake_catalog_handler_route_census.py; if unexpected, the "
            f"FakeCatalogHandler route(s) this method backs may now be stale."
        )
    return match.start()


def _extract_method_body(source: str, method_name: str) -> str:
    """Extract a Java method's ``{ ... }`` body via brace-balance scanning
    (not a regex match for the closing brace, which cannot reliably span
    arbitrary nesting depth). Skips braces inside string/char literals so a
    JSON-shaped string constant elsewhere in the body can't desync the count.
    """
    sig_start = _find_method_body_start(source, method_name)
    brace_start = source.index("{", sig_start)
    depth = 0
    in_string = False
    in_char = False
    i = brace_start
    n = len(source)
    while i < n:
        c = source[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
        elif in_char:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_char = False
        elif c == '"':
            in_string = True
        elif c == "'":
            in_char = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start : i + 1]
        i += 1
    raise AssertionError(
        f"unbalanced braces scanning {method_name}()'s body starting at "
        f"offset {brace_start} — parser bug, or the method body contains a "
        f"construct (e.g. a text block) this scanner doesn't handle; "
        f"inspect CatalogRepository.java around that offset"
    )


def _extract_put_keys(method_body: str) -> set[str]:
    keys = set(_PUT_KEY_RE.findall(method_body))
    if not keys:
        raise AssertionError(
            "parsed ZERO `.put(\"key\", ...)` keys from the method body — "
            "either the method no longer builds its result via .put() calls "
            "(rewritten to a different construct), or the regex needs "
            "updating. Empty census = vacuous pass (nexus-0948s empty-census "
            "trap)."
        )
    return keys


def _leaf_dict_keys(node: ast.Dict) -> set[str]:
    """FakeCatalogHandler's ``_send_json({...})`` payloads follow one of two
    shapes: a bare object (``/collections/get`` — return the row directly),
    or a single-key envelope wrapping a list of rows (``/collections/list``
    -> ``{"collections": [row, ...]}``, ``/docs/orphaned`` ->
    ``{"documents": [row, ...]}``). For the envelope shape we want the ROW's
    keys (what CatalogRepository.java's row-builder actually produces), not
    the wrapper key — so unwrap one level when this dict is exactly that
    shape.
    """
    if (
        len(node.keys) == 1
        and isinstance(node.values[0], ast.List)
        and node.values[0].elts
        and isinstance(node.values[0].elts[0], ast.Dict)
    ):
        node = node.values[0].elts[0]
    return {
        k.value
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _fake_route_response_keys(fake_source: str, route: str) -> set[str]:
    """Return the JSON key set FakeCatalogHandler sends for `op == route`.

    Walks the AST (not regex) so nested if/else within a single route branch
    (e.g. ``/collections/get``'s missing-name 400 vs its 200 success body)
    resolve correctly: among every literal-dict ``self._send_json({...})``
    call found within the branch, the largest key set wins (error bodies are
    always the degenerate ``{"error": ...}`` 1-key shape; the real response
    shape is never smaller than an error stub for these routes).
    """
    tree = ast.parse(fake_source)
    class_node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "FakeCatalogHandler"
        ),
        None,
    )
    if class_node is None:
        raise AssertionError(
            "could not find `class FakeCatalogHandler` via ast.parse — the "
            "class was renamed or moved"
        )
    do_methods = [
        n
        for n in class_node.body
        if isinstance(n, ast.FunctionDef) and n.name in ("do_GET", "do_POST")
    ]
    if not do_methods:
        raise AssertionError(
            "FakeCatalogHandler has neither do_GET nor do_POST — dispatch "
            "was rewritten to a different method name"
        )

    def branch_matches(test: ast.expr) -> bool:
        return (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "op"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == route
        )

    def find_branch(node: ast.If) -> ast.If | None:
        if branch_matches(node.test):
            return node
        # `elif` is represented as a nested If in `.orelse` — walk ONLY that
        # chain (never `.body`) to find sibling elif branches without
        # descending into an unrelated branch's own internal if/else.
        for stmt in node.orelse:
            if isinstance(stmt, ast.If):
                found = find_branch(stmt)
                if found is not None:
                    return found
        return None

    matched: ast.If | None = None
    for method in do_methods:
        for stmt in method.body:
            if isinstance(stmt, ast.If):
                matched = find_branch(stmt)
                if matched is not None:
                    break
        if matched is not None:
            break

    if matched is None:
        raise AssertionError(
            f"could not locate an `op == {route!r}` branch in "
            f"FakeCatalogHandler's do_GET/do_POST — the route was renamed "
            f"or removed from the fake"
        )

    # Walk ONLY this branch's own true-body (which legitimately includes any
    # NESTED if/else written inline, e.g. /collections/get's name-required
    # check) — deliberately NOT `matched.orelse`, which for a top-level
    # `elif` node holds the REST of the elif chain (sibling routes), not
    # part of this branch.
    best: set[str] = set()
    for body_stmt in matched.body:
        for node in ast.walk(body_stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_send_json"
                and node.args
                and isinstance(node.args[0], ast.Dict)
            ):
                keys = _leaf_dict_keys(node.args[0])
                if len(keys) > len(best):
                    best = keys

    if not best:
        raise AssertionError(
            f"found the `op == {route!r}` branch but no literal-dict "
            f"`self._send_json({{...}})` call inside it with any string "
            f"keys — empty census trap (nexus-0948s)"
        )
    return best


#: The 3 CatalogRepository.java row-builder methods the 2026-07-04 critique
#: manually cross-referenced against FakeCatalogHandler and found accurate
#: ("Cross-referenced 3 hand-authored wire shapes ... against the ACTUAL
#: current CatalogRepository.java — all three accurate today.") This
#: mechanizes exactly that manual check so its accuracy doesn't silently
#: expire the next time either side changes.
FIELD_SHAPE_CHECKS: list[tuple[str, str]] = [
    ("stats", "/stats"),
    ("collRow", "/collections/list"),
    ("collRow", "/collections/get"),
    ("orphanedDocs", "/docs/orphaned"),
]


@pytest.mark.parametrize(
    ("java_method", "fake_route"), FIELD_SHAPE_CHECKS, ids=lambda v: v
)
def test_field_shape_parity_for_hand_verified_methods(
    java_method: str, fake_route: str
) -> None:
    """Mechanizes the 2026-07-04 critique's manual field-level cross-check.

    The route census above (test 1) only proves a route EXISTS on both
    sides — it says nothing about whether the JSON keys match, which is
    precisely the h8rf6.3 incident class (a shape mismatch invisible to
    every route-existence / MagicMock-consumer check) one layer down at the
    wire level. This test extracts the literal key set each side actually
    produces and asserts equality.
    """
    java_source = _read(_JAVA_REPOSITORY)
    fake_source = _read(_FAKE_HANDLER)

    method_body = _extract_method_body(java_source, java_method)
    java_keys = _extract_put_keys(method_body)
    fake_keys = _fake_route_response_keys(fake_source, fake_route)

    assert java_keys == fake_keys, (
        f"{java_method}() in CatalogRepository.java builds keys "
        f"{sorted(java_keys)} but FakeCatalogHandler's {fake_route!r} route "
        f"sends {sorted(fake_keys)}.\n"
        f"missing from fake: {sorted(java_keys - fake_keys)}\n"
        f"extra in fake (no Java field): {sorted(fake_keys - java_keys)}\n"
        f"WHAT changed: a field was added, removed, or renamed on the Java "
        f"side (or the fake drifted) without the other side following.\n"
        f"WHETHER IT MATTERS: yes — {java_method}() backs a live wire route "
        f"any test hitting the fake server relies on; a field-level mismatch "
        f"is invisible to test_shape_parity_tripwire.py's dataclass-collapsed "
        f"shape() descriptor (dicts there compare recursively but only if "
        f"the REGISTRY entry's seeded call surfaces the differing key), so "
        f"this is the mechanized backstop for exactly the drift class the "
        f"2026-07-04 critique caught by hand.\n"
        f"HOW TO FIX: update FakeCatalogHandler's {fake_route!r} branch "
        f"(tests/catalog/test_http_catalog_client.py) to add/remove/rename "
        f"the matching key so it mirrors {java_method}()'s current shape."
    )


def test_field_shape_checks_reference_real_routes() -> None:
    """Guard the guard: every FIELD_SHAPE_CHECKS route must be a route this
    module's own census (test 1) actually knows about — catches a typo'd or
    stale route name in FIELD_SHAPE_CHECKS silently degrading to a
    KeyError-free but meaningless comparison.
    """
    fake_routes = _extract_fake_routes(_read(_FAKE_HANDLER))
    referenced = {route for _, route in FIELD_SHAPE_CHECKS}
    unknown = referenced - fake_routes
    assert not unknown, (
        f"FIELD_SHAPE_CHECKS references route(s) not found in "
        f"FakeCatalogHandler's do_GET/do_POST: {sorted(unknown)} — typo, or "
        f"the route was renamed/removed"
    )
