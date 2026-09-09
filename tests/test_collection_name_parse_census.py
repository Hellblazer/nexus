# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Client census of raw collection-name parsing (RDR-204 Phase 3 item 1,
nexus-ft04v.20). ACCEPTANCE SIGNAL 2 of 2.

THE BUG CLASS THIS FREEZES. Collection identity (content type, model,
lifecycle state) has moved to ``catalog_collections`` as the single source
of truth (RDR-204 Phase 1/2). Client code that still reaches for the
metadata by string-splitting the collection NAME reads a fact the row
already carries, and drifts from the row the moment a name and its row
disagree (the exact defect class RDR-204 exists to close). This gate does
not fix a single site — Phase 3 items 2-5 (the funnel slices, nexus-ft04v.21
through .23) do that, each lowering the pin here. This gate exists FIRST,
pinned at the count measured BEFORE any of those slices land, so every
later slice's acceptance is "the pin fell", not a claim nobody can check.

FOUR PATTERN CLASSES, per RDR-204 Phase 3 item 1 (docs/rdr/
rdr-204-embedding-profile-and-collection-authority.md, sections
"Implementation Plan / Phase 3" and "Validation / Testing Strategy"):
``split("__")`` / ``rsplit("__")``, ``partition("__")`` / ``rpartition("__")``,
``startswith(<type>__ or "quarantine-")`` / ``endswith(...)`` (bare or in a
tuple), and ``"__" in x`` / ``"__" not in x``. All four are AST-matched
(``ast.Call`` / ``ast.Compare`` nodes), not grepped — a comment or docstring
containing the string ``"__"`` must never count.

WHY AST, NOT GREP. The originating bead's own prior grep measured 63 sites
under the first three classes and needed correcting twice: once because it
missed the fourth class entirely (at least two live sites, including
commands/store.py:672, are matched ONLY by ``"__" in``), and once because
four of the 63 parse ``mcp__`` TOOL names, not collection names, and were
never filtered. A grep re-measurement drifts the same way again the next
time the tree moves; an AST walk with an explicit, tested exclusion list
does not.

RE-MEASURED ON THIS TREE (2026-09-09, commit a821d6622): the four classes
match 83 raw sites across 27 files. FOUR are excluded (all four rsplit(__)
sites in the entire tree, and only those) because they strip an
``mcp__...__`` tool-name prefix, not a collection name -- see
``_EXCLUDED_SITES`` below, each with its own reason and each independently
verified (``test_excluded_sites_are_real_matches_not_omissions``) to be a
genuine raw match rather than a stale or invented entry. The pin is
79 == 83 - 4.

RDR-204'S OTHER NAMED EXCLUSION -- ``rdr-`` document ids -- MATCHES ZERO
SITES HERE. The one ``rdr-`` id parse in the tree,
``catalog/rdr_canonical.py:124``'s ``name.lower().startswith("rdr-")``,
uses the literal ``"rdr-"`` (single hyphen), which is not in
``_TYPE_PREFIXES`` (``"rdr__"``, double underscore, is a collection prefix;
``"rdr-"`` is an RDR document id) -- it was never going to be picked up by
this scanner's ``startswith``/``endswith`` matcher in the first place. This
is recorded as a checked zero, not a silent omission
(``test_rdr_id_parse_is_not_a_scanner_hit``), exactly the "kept at 0 rather
than dropped" discipline of the private-handle census this gate is modelled
on (``tests/test_private_handle_access_census.py``).

WHY A CENSUS-IN-TEST-FILE RATHER THAN A ``src/`` LINT MODULE (the
alternative the bead names, ``storage_boundary_lint.py``-style). Nothing in
``src/nexus`` needs to call this scanner at runtime or share it across
modules -- it exists only to be asserted against in this file, exactly like
``tests/test_private_handle_access_census.py``. A ``src/`` module would add
an import surface with no production consumer.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

#: Collection-type / lifecycle prefixes a raw ``startswith``/``endswith``
#: call names when it is parsing a collection NAME rather than checking an
#: unrelated string. Per RDR-204 Phase 3 item 1's four pattern classes.
_TYPE_PREFIXES = frozenset({"code__", "docs__", "rdr__", "knowledge__", "quarantine-"})

#: (relative-path, lineno) -> one-line reason, for sites the raw AST scan
#: WOULD flag but which the RDR-204 Phase 3 item 1 exclusion rule (``mcp__``
#: tool names, ``rdr-`` document ids) removes from the pin. Every entry here
#: is independently verified as a real raw match by
#: ``test_excluded_sites_are_real_matches_not_omissions`` — an exclusion
#: that no longer matches anything is a stale entry, not a quieter gate.
_EXCLUDED_SITES: dict[tuple[str, int], str] = {
    # Line moved 7887 -> 7900 when nexus-ft04v.21 funnelled this file's
    # other three sites (2451, 2460, 7622) and the new imports/comments
    # shifted everything below them.
    ("src/nexus/mcp/core.py", 7900): (
        "mcp__ tool name: `raw_tool.rsplit(\"__\", 1)[-1] if "
        'raw_tool.startswith("mcp__")` strips an MCP tool-name prefix for '
        "planner-step normalization, not a collection name."
    ),
    ("src/nexus/plans/bundle.py", 230): (
        "mcp__ tool name: `_extract_tool_name`'s own docstring says it "
        '"strips any mcp__...__ prefix" from a plan step\'s tool identifier.'
    ),
    ("src/nexus/plans/cost_estimate.py", 447): (
        "mcp__ tool name: `_extract_tool`, the documented local copy of "
        "bundle._extract_tool_name, strips the same mcp__...__ prefix."
    ),
    ("src/nexus/plans/runner.py", 2377): (
        "mcp__ tool name: strips an mcp__...__ prefix from a resolved plan "
        "step's tool identifier before dispatch, guarded by the same "
        '`startswith("mcp__")` check as the other three sites.'
    ),
}

#: 2026-09-09 census of collection-name parse sites, per file, AFTER the
#: `_EXCLUDED_SITES` filter. This may only shrink — each of
#: nexus-ft04v.21/.22/.23 (the funnel slices) lowers the entries for its
#: files as raw sites move to the three CollectionName helpers; when a
#: file reaches zero, drop its entry (a dropped key and an absent file
#: both read as zero to the guards below).
#:
#: nexus-ft04v.21 (funnel slice 1, corpus.py/scoring.py/collection_shape.py/
#: context.py/search_engine.py/exporter.py/mcp/core.py) landed 2026-09-09
#: and lowered the pin from 79 to 54 (-25): scoring.py, context.py,
#: search_engine.py, exporter.py and mcp/core.py reached zero and were
#: dropped; corpus.py fell from 16 to 2 (the two `collection_content_type`
#: / `collection_owner` helpers' own shared internal parse -- see
#: `_split_legacy_collection_name` in nexus/corpus.py -- is the accepted
#: floor those two helpers leave behind, per the bead's "sites inside the
#: three helpers themselves ... stay counted" rule). collection_shape.py's
#: 3 sites (188, 191, 195) were LEFT UNFUNNELLED, not overlooked, per the
#: coordinator's ruling on nexus-ft04v.21's hand-off report: `collection_
#: attributes`'s positional 4-field decode of a malformed name is the same
#: class as a commands/collection.py slice-3 site -- the funnel helpers'
#: "owner = whole remainder" convention cannot reproduce a strict
#: positional decode byte-for-byte, and this bead forbids behaviour
#: change. These stay raw and counted until nexus-ft04v.26 (the repoint),
#: where the decode reads the catalog row directly instead of parsing.
COLLECTION_NAME_PARSE_CENSUS: dict[str, int] = {
    "src/nexus/catalog/chunk_quarantine.py": 1,
    "src/nexus/catalog/orphan_backfill.py": 1,
    "src/nexus/catalog/recovery_bundle.py": 1,
    "src/nexus/collection_shape.py": 3,
    "src/nexus/commands/catalog.py": 10,
    "src/nexus/commands/catalog_cmds/integrity.py": 1,
    "src/nexus/commands/catalog_cmds/migration.py": 2,
    "src/nexus/commands/catalog_cmds/reconcile_stale.py": 2,
    "src/nexus/commands/collection.py": 10,
    "src/nexus/commands/command_context.py": 1,
    "src/nexus/commands/doctor.py": 1,
    "src/nexus/commands/enrich.py": 1,
    "src/nexus/commands/index.py": 5,
    "src/nexus/commands/store.py": 1,
    "src/nexus/corpus.py": 2,
    "src/nexus/db/embed_migrate.py": 4,
    "src/nexus/db/http_vector_client.py": 3,
    "src/nexus/db/reconcile.py": 3,
    "src/nexus/db/t3.py": 2,
}

PARSE_SITE_PIN: int = sum(COLLECTION_NAME_PARSE_CENSUS.values())


def _get_str_const(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _scan_tree(path: pathlib.Path) -> list[tuple[int, str]]:
    """(lineno, pattern_class) for every raw collection-name-parse-shaped
    site in one file -- BEFORE the ``_EXCLUDED_SITES`` filter is applied.

    AST-based by construction: a match requires an actual ``ast.Call`` to
    ``split``/``rsplit``/``partition``/``rpartition``/``startswith``/
    ``endswith`` with a literal argument, or an actual ``ast.Compare`` with
    ``In``/``NotIn`` against the literal ``"__"`` -- never a text match, so
    a comment or docstring mentioning ``"__"`` cannot contribute a hit.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("split", "rsplit", "partition", "rpartition"):
                if node.args and _get_str_const(node.args[0]) == "__":
                    hits.append((node.lineno, attr))
            elif attr in ("startswith", "endswith"):
                matched = False
                for arg in node.args:
                    sv = _get_str_const(arg)
                    if sv is not None and sv in _TYPE_PREFIXES:
                        matched = True
                    elif isinstance(arg, (ast.Tuple, ast.List)):
                        for elt in arg.elts:
                            if _get_str_const(elt) in _TYPE_PREFIXES:
                                matched = True
                if matched:
                    hits.append((node.lineno, attr))
        elif isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                if _get_str_const(node.left) == "__" or _get_str_const(comparator) == "__":
                    hits.append((node.lineno, "not_in" if isinstance(op, ast.NotIn) else "in"))
    return hits


def _raw_collection_name_parse_sites() -> dict[str, list[tuple[int, str]]]:
    """Every raw match under SRC, keyed by path relative to REPO_ROOT,
    with NO exclusion filter applied. Used to prove exclusions are real."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        hits = _scan_tree(path)
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def _collection_name_parse_sites() -> dict[str, list[tuple[int, str]]]:
    """Raw matches under SRC with `_EXCLUDED_SITES` filtered out -- this is
    what the pin counts."""
    result: dict[str, list[tuple[int, str]]] = {}
    for rel, hits in _raw_collection_name_parse_sites().items():
        kept = [(lineno, cls) for lineno, cls in hits if (rel, lineno) not in _EXCLUDED_SITES]
        if kept:
            result[rel] = kept
    return result


def _grown(live: dict[str, int], census: dict[str, int]) -> list[str]:
    return sorted(
        f"{f}: {live.get(f, 0)} > {census.get(f, 0)}"
        for f in live.keys() | census.keys()
        if live.get(f, 0) > census.get(f, 0)
    )


def _shrunk(live: dict[str, int], census: dict[str, int]) -> list[str]:
    return sorted(
        f"{f}: {live.get(f, 0)} < {census.get(f, 0)}"
        for f in live.keys() | census.keys()
        if live.get(f, 0) < census.get(f, 0)
    )


def test_scanner_is_not_vacuous(tmp_path: pathlib.Path) -> None:
    """Prove the AST walk actually matches all four classes, and that the
    TYPE_PREFIXES filter is not accidentally permissive, on a sample with a
    known answer -- independent of how large the live debt happens to be."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f(name, other):\n"
        '    a = name.split("__", 1)\n'          # split: MUST match
        '    b = name.rsplit("__", 1)\n'         # rsplit: MUST match
        '    c = name.partition("__")\n'         # partition: MUST match
        '    d = name.rpartition("__")\n'        # rpartition: MUST match
        '    e = name.startswith("code__")\n'    # startswith: MUST match
        '    g = name.endswith(("docs__", "rdr__"))\n'  # endswith tuple: MUST match
        '    h = "__" in name\n'                 # in: MUST match
        '    i = "__" not in name\n'             # not in: MUST match
        '    j = name.startswith("mcp__")\n'     # not a type prefix: must NOT match
        '    k = name.split("-", 1)\n'           # wrong literal: must NOT match
        "    return a, b, c, d, e, g, h, i, j, k\n",
        encoding="utf-8",
    )
    hits = _scan_tree(sample)
    classes = [cls for _lineno, cls in hits]
    assert classes == [
        "split", "rsplit", "partition", "rpartition",
        "startswith", "endswith", "in", "not_in",
    ], (
        f"scanner drifted from the four RDR-204 Phase 3 item 1 pattern "
        f"classes: {hits}. Either a real class stopped matching, or the "
        f"mcp__/wrong-literal negative cases started matching -- both make "
        f"every guard below pass by doing nothing or by over-counting."
    )


def test_scanner_still_sees_the_live_tree() -> None:
    """Companion to the synthetic check: the walk is actually pointed at
    src/nexus, not an empty or missing directory."""
    assert any(SRC.rglob("*.py")), f"SRC does not resolve to python sources: {SRC}"


def test_excluded_sites_are_real_matches_not_omissions() -> None:
    """Every _EXCLUDED_SITES entry must be a genuine raw hit at that exact
    line. An exclusion that stops matching (the line moved, the code
    changed) is a stale entry masquerading as a live judgement call --
    this is what tells a later reader "excluded" apart from "forgotten"."""
    raw = _raw_collection_name_parse_sites()
    missing = [
        f"{rel}:{lineno}"
        for (rel, lineno) in _EXCLUDED_SITES
        if lineno not in {ln for ln, _cls in raw.get(rel, [])}
    ]
    assert not missing, (
        f"declared exclusion(s) no longer match any raw site: {missing}. "
        "Either the line moved (update the lineno) or the code changed and "
        "the exclusion should be deleted -- a stale exclusion silently "
        "widens the census's blind spot without lowering the pin."
    )


def test_excluded_sites_do_not_reach_the_pin() -> None:
    """The filter actually removes what it claims to: no excluded
    (rel, lineno) pair survives into the pinned census."""
    live = _collection_name_parse_sites()
    leaked = [
        f"{rel}:{lineno}"
        for (rel, lineno) in _EXCLUDED_SITES
        if lineno in {ln for ln, _cls in live.get(rel, [])}
    ]
    assert not leaked, f"excluded site(s) still counted toward the pin: {leaked}"


def test_rdr_id_parse_is_not_a_scanner_hit() -> None:
    """RDR-204's other named exclusion -- rdr- document ids -- is a checked
    zero, not an unexamined one. The one rdr- id parse in the tree uses the
    literal "rdr-" (single hyphen), which is not in _TYPE_PREFIXES ("rdr__",
    double underscore, is the collection prefix); confirm it truly does not
    reach the scanner, rather than assuming the literal difference is
    enough without ever running the scan against it."""
    target = SRC / "catalog" / "rdr_canonical.py"
    assert target.exists(), f"reference file moved or was deleted: {target}"
    hits = {lineno for lineno, _cls in _scan_tree(target)}
    assert 124 not in hits, (
        "catalog/rdr_canonical.py:124's rdr- id check now matches the "
        "scanner (the literal or the call shape changed) -- if it is a "
        "genuine rdr- document-id parse, add it to _EXCLUDED_SITES with "
        "that reason rather than letting it inflate the pin silently."
    )


def test_synthetic_new_parse_site_fails_the_growth_guard() -> None:
    """A new (or grown) site is what test_no_new_parse_sites exists to
    catch -- proven here against a synthetic dict, decoupled from whatever
    the live tree happens to contain today."""
    some_file = next(iter(COLLECTION_NAME_PARSE_CENSUS))
    live = dict(COLLECTION_NAME_PARSE_CENSUS)
    live[some_file] += 1
    grown = _grown(live, COLLECTION_NAME_PARSE_CENSUS)
    assert grown == [f"{some_file}: {live[some_file]} > {COLLECTION_NAME_PARSE_CENSUS[some_file]}"]

    live_new_file = dict(COLLECTION_NAME_PARSE_CENSUS)
    live_new_file["src/nexus/not_a_real_file.py"] = 1
    assert _grown(live_new_file, COLLECTION_NAME_PARSE_CENSUS) == [
        "src/nexus/not_a_real_file.py: 1 > 0"
    ]


def test_synthetic_shrink_without_lowering_pin_fails_the_stale_guard() -> None:
    """Removing a site without lowering its file's pin is the OTHER half
    of reduce-only discipline -- test_census_has_no_stale_entries exists to
    catch a pin that overstates reality, proven here against a synthetic
    dict for the same reason as the growth guard above."""
    some_file = next(f for f, n in COLLECTION_NAME_PARSE_CENSUS.items() if n > 0)
    live = dict(COLLECTION_NAME_PARSE_CENSUS)
    live[some_file] -= 1
    shrunk = _shrunk(live, COLLECTION_NAME_PARSE_CENSUS)
    assert shrunk == [f"{some_file}: {live[some_file]} < {COLLECTION_NAME_PARSE_CENSUS[some_file]}"]


def test_no_new_parse_sites() -> None:
    """THE GUARD. A new raw collection-name parse site outside the three
    RDR-204 helpers is exactly the regression Phase 3 exists to prevent --
    each funnel slice is supposed to LOWER this census, never add to it."""
    live = {f: len(h) for f, h in _collection_name_parse_sites().items()}
    grown = _grown(live, COLLECTION_NAME_PARSE_CENSUS)
    assert not grown, (
        f"collection-name parse sites GREW at {grown}.\n"
        "RDR-204 moved collection identity (content type, model, lifecycle "
        "state) to catalog_collections; a new raw split/partition/"
        "startswith/\"__\" in\" parse re-derives a fact the row already "
        "carries and can drift from it (docs/rdr/"
        "rdr-204-embedding-profile-and-collection-authority.md § "
        "Implementation Plan Phase 3). Use the three CollectionName "
        "helpers the funnel slices (nexus-ft04v.21/.22/.23) route existing "
        "sites through instead. If this is a genuine mcp__ tool-name or "
        "rdr- document-id parse, add it to _EXCLUDED_SITES with the guard "
        "named."
    )


def test_census_has_no_stale_entries() -> None:
    """Exact-census discipline, matching test_no_new_sqlite and
    test_private_handle_access_census's own stale-entry guard: a pin that
    overstates the live tree is a lie about how much of Phase 3 is left,
    and hides a later regrowth inside the slack."""
    live = {f: len(h) for f, h in _collection_name_parse_sites().items()}
    shrunk = _shrunk(live, COLLECTION_NAME_PARSE_CENSUS)
    assert not shrunk, (
        f"stale census entry {shrunk}: the site was removed (good, a "
        "funnel slice landed) -- lower the count so the pin stays exact."
    )


def test_pin_matches_documented_total() -> None:
    """The PARSE_SITE_PIN docstring claim (54, after nexus-ft04v.21's
    funnel slice 1) is derived from the same dict the guards above check
    against -- this catches a hand-edited docstring number drifting from
    the dict it claims to summarize."""
    assert PARSE_SITE_PIN == sum(COLLECTION_NAME_PARSE_CENSUS.values())
    assert PARSE_SITE_PIN == 54
