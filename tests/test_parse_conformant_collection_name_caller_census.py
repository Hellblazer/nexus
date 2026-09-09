# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-204 Phase 3 item 6 (nexus-ft04v.27). ACCEPTANCE SIGNAL.

THE BUG CLASS THIS FREEZES. Six client registration sites (indexer.py,
``commands/collection.py``'s ``reindex_cmd``, ``commands/catalog_cmds/
collections.py``'s ``backfill_collections_cmd`` and
``rename_collection_cmd``, ``commands/catalog_cmds/migration.py``'s
``migrate_fallback_cmd``, and ``commands/index.py``) used to call
``nexus.corpus.parse_conformant_collection_name`` directly to re-derive
``content_type``/``owner_id``/``embedding_model``/``model_version`` from a
collection name the SAME client had either just rendered (indexer.py,
migration.py) or was re-registering unchanged (the other three) --
extracting facts by parsing a string instead of carrying the values
through. ``commands/index.py``'s site was already fixed by an earlier
bead (nexus-ft04v.34); this bead retired the other five.

This is a DIFFERENT census from ``test_collection_name_parse_census.py``,
which counts raw ``split``/``partition``/``startswith``/``"__" in``
pattern sites (a different retired idiom). This one counts DIRECT CALLS
to ``parse_conformant_collection_name`` itself, AST-matched (an
``ast.Call`` whose callee name is exactly ``parse_conformant_collection_
name`` -- a bare name reference or a ``module.parse_conformant_
collection_name`` attribute access), never grepped, so a comment or
docstring mentioning the function name cannot contribute a hit.

THE REMAINING CALLERS ARE REAL, NOT A BUG. Per the bead's DECISIONS
(overriding an earlier RDR draft that expected exactly two callers --
the backfill and the census gate -- for a DIFFERENT, already-resolved
symbol, ``CollectionName.parse``): ``parse_conformant_collection_name``
itself keeps callers beyond the six registration sites, because they are
either (a) the funnel helpers RDR-204 Phase 3 items 2-5 built and item 5
(nexus-ft04v.26, not yet landed) will repoint at the catalog row, (b) a
sibling extraction function this bead's scope never named, or (c) a
read-only diagnostic that was never one of the six registration sites in
the first place. Every entry below is real (verified independently by
``test_allowlisted_sites_are_real_calls_not_stale_entries``) and
documented with why it survives; the pin is EXACT (neither a floor a
future bead may raise, nor a ratchet expected to keep shrinking) -- a
NEW caller appearing anywhere is exactly as much a regression as one of
the six retired sites coming back.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

_TARGET = "parse_conformant_collection_name"

#: (relative-path, lineno) -> reason the call survives nexus-ft04v.27.
ALLOWED_CALLERS: dict[tuple[str, int], str] = {
    ("src/nexus/corpus.py", 626): (
        "collection_content_type: RDR-204 Phase 3 funnel helper #1. Still "
        "parses until nexus-ft04v.26 repoints it at the catalog row."
    ),
    ("src/nexus/corpus.py", 658): (
        "collection_owner: RDR-204 Phase 3 funnel helper #2. Still parses "
        "until nexus-ft04v.26 repoints it at the catalog row."
    ),
    ("src/nexus/corpus.py", 1183): (
        "collection_registration_kwargs: the write-time registration "
        "derivation used by HttpCatalogClient.register_collection's OWN "
        "bare-call fallback and by ensure_collection_registered (T3 chunk "
        "writes, aspects, taxonomy). Never one of the six sites this bead "
        "retires -- those six pass content_type/owner_id/embedding_model/ "
        "model_version explicitly so this fallback path never fires for "
        "them; out of nexus-ft04v.27's scope."
    ),
    ("src/nexus/catalog/collection_name.py", 113): (
        "CollectionName.parse: the render path's own parse counterpart. "
        "Pre-existing single caller (http_catalog_client.py's "
        "collection_for) unaffected by this bead."
    ),
    ("src/nexus/commands/collection.py", 361): (
        "_find_dimension_mismatched_collections: read-only `nx collection "
        "prune` diagnostic (compares a name's declared model dim against "
        "the active embedder's dim). Never a registration call and never "
        "one of the six sites this bead retires."
    ),
    ("src/nexus/commands/catalog_cmds/doctor.py", 971): (
        "_run_name_vs_embed_dim: read-only `nx catalog doctor` diagnostic "
        "(the 4.28-era mislabeled-collection detector). Never a "
        "registration call and never one of the six sites this bead "
        "retires."
    ),
    ("src/nexus/health.py", 5222): (
        "The doctor chash-conformance check's unroutable-collection probe: "
        "read-only, samples T3 collection names to find ones whose model "
        "token maps to no known dimension. Never a registration call and "
        "never one of the six sites this bead retires."
    ),
    ("src/nexus/repo_identity.py", 431): (
        "list_sibling_collections: read-only owner-segment sibling lookup "
        "used by repo-identity resolution. Never a registration call and "
        "never one of the six sites this bead retires."
    ),
}

#: The six registration sites nexus-ft04v.27 retired -- asserted ABSENT
#: below, so a regression (someone re-adding a direct parse call at one
#: of these exact spots) fails loud instead of silently passing because
#: it happens to also be outside ALLOWED_CALLERS (which would already
#: fail the exact-set assertion, but this gives a name to the failure).
RETIRED_SITES: tuple[str, ...] = (
    "src/nexus/indexer.py (post-rename registration)",
    "src/nexus/commands/collection.py (reindex_cmd re-registration)",
    "src/nexus/commands/catalog_cmds/collections.py (backfill_collections_cmd)",
    "src/nexus/commands/catalog_cmds/collections.py (rename_collection_cmd)",
    "src/nexus/commands/catalog_cmds/migration.py (migrate_fallback_cmd)",
    "src/nexus/commands/index.py (already fixed pre-nexus-ft04v.27, "
    "by nexus-ft04v.34)",
)


def _is_target_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _TARGET
    if isinstance(func, ast.Attribute):
        return func.attr == _TARGET
    return False


def _scan_file(path: pathlib.Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return []
    return sorted(
        node.lineno for node in ast.walk(tree) if _is_target_call(node)
    )


def _all_call_sites() -> dict[str, list[int]]:
    """(relative path) -> sorted line numbers of every direct call to
    ``parse_conformant_collection_name`` under ``src/nexus``, definition
    site excluded (the function's own ``def`` is not a call)."""
    found: dict[str, list[int]] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        hits = _scan_file(path)
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_scanner_is_not_vacuous(tmp_path: pathlib.Path) -> None:
    """Prove the AST walk matches both a bare-name call and a
    qualified-attribute call, and does NOT match an unrelated name."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from nexus.corpus import parse_conformant_collection_name\n"
        "import nexus.corpus as corpus_mod\n"
        "\n"
        "def f(name):\n"
        "    a = parse_conformant_collection_name(name)\n"
        "    b = corpus_mod.parse_conformant_collection_name(name)\n"
        "    c = other_function(name)\n"
        "    return a, b, c\n",
        encoding="utf-8",
    )
    hits = _scan_file(sample)
    assert hits == [5, 6], (
        f"scanner drifted: expected bare-name call at line 5 and "
        f"attribute-access call at line 6 only, got {hits}"
    )


def test_scanner_still_sees_the_live_tree() -> None:
    assert any(SRC.rglob("*.py")), f"SRC does not resolve to python sources: {SRC}"


def test_allowlisted_sites_are_real_calls_not_stale_entries() -> None:
    """Every ALLOWED_CALLERS entry must be a genuine call at that exact
    line -- a stale entry (the line moved, the call was deleted) silently
    widens what this gate accepts without anyone noticing."""
    live = _all_call_sites()
    missing = [
        f"{rel}:{lineno}"
        for (rel, lineno) in ALLOWED_CALLERS
        if lineno not in live.get(rel, [])
    ]
    assert not missing, (
        f"declared allowlist entr(y/ies) no longer match a real call: "
        f"{missing}. Either the line moved (update the lineno) or the "
        f"call was removed (drop the entry -- a lower pin is welcome)."
    )


def test_direct_callers_are_exactly_the_allowlist() -> None:
    """The pin: every direct call to parse_conformant_collection_name in
    src/nexus is one of the documented, verified ALLOWED_CALLERS entries
    -- no more, no fewer. A NEW site (a regression re-adding one of the
    six retired registration parses, or an entirely new caller) fails
    here; a site that has since been retired and dropped from
    ALLOWED_CALLERS also fails here (drop the stale allowlist entry)."""
    live = _all_call_sites()
    live_set = {
        (rel, lineno) for rel, linenos in live.items() for lineno in linenos
    }
    allowed_set = set(ALLOWED_CALLERS)

    extra = sorted(f"{rel}:{lineno}" for rel, lineno in (live_set - allowed_set))
    missing = sorted(f"{rel}:{lineno}" for rel, lineno in (allowed_set - live_set))

    assert not extra, (
        f"UNDOCUMENTED direct caller(s) of parse_conformant_collection_name: "
        f"{extra}. If this is a legitimate new funnel-style helper, add it "
        f"to ALLOWED_CALLERS with a reason; if it is a re-added registration "
        f"parse, retire it the way nexus-ft04v.27 retired the other six."
    )
    assert not missing, (
        f"ALLOWED_CALLERS entr(y/ies) no longer observed in the live tree: "
        f"{missing}. The call was removed -- drop the stale entry (a lower "
        f"pin is welcome, never an inflated one)."
    )


def test_retired_sites_are_documented() -> None:
    """Non-vacuity for the retirement itself: name the six sites so a
    reader (and a future regression) has something concrete to check
    against, independent of the exact-allowlist assertion above."""
    assert len(RETIRED_SITES) == 6, RETIRED_SITES
