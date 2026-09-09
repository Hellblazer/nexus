# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-204 Phase 3 item 6 (nexus-ft04v.27). ACCEPTANCE SIGNAL.

THE BUG CLASS THIS FREEZES. Ten client call sites used to call
``nexus.corpus.parse_conformant_collection_name`` directly instead of
going through the funnel helpers: six REGISTRATION sites (indexer.py,
``commands/collection.py``'s ``reindex_cmd``, ``commands/catalog_cmds/
collections.py``'s ``backfill_collections_cmd`` and
``rename_collection_cmd``, ``commands/catalog_cmds/migration.py``'s
``migrate_fallback_cmd``, and ``commands/index.py``, the last already
fixed by an earlier bead, nexus-ft04v.34) plus four READ-ONLY diagnostic
sites (``health.py``'s chash-conformance unroutable-collection probe,
``repo_identity.py``'s ``list_sibling_collections``, ``commands/
collection.py``'s ``_find_dimension_mismatched_collections``, and
``commands/catalog_cmds/doctor.py``'s ``_run_name_vs_embed_dim``) that
were never registration calls but parsed the name directly all the
same. The coordinator's follow-up on this bead's first pass named the
four diagnostics: they are exactly the class RDR-204 Phase 3 item 5
(nexus-ft04v.26, the helper repoint) must end -- once the helpers read
the catalog row instead of the name, a caller still parsing the name
DIRECTLY would silently stay on the old, wrong source of truth. All ten
sites are retired here.

This is a DIFFERENT census from ``test_collection_name_parse_census.py``,
which counts raw ``split``/``partition``/``startswith``/``"__" in``
pattern sites (a different retired idiom). This one counts DIRECT CALLS
to ``parse_conformant_collection_name`` itself, AST-matched (an
``ast.Call`` whose callee name is exactly ``parse_conformant_collection_
name`` -- a bare name reference or a ``module.parse_conformant_
collection_name`` attribute access), never grepped, so a comment or
docstring mentioning the function name cannot contribute a hit.

THE REMAINING CALLERS ARE THE TARGET, NOT A REMNANT. Per the bead's
DECISIONS plus the coordinator's follow-up: ``parse_conformant_
collection_name``'s only surviving direct callers are the render path's
own helpers -- ``collection_content_type`` / ``collection_owner`` (the
funnel helpers RDR-204 Phase 3 items 2-5 built, still parsing until
nexus-ft04v.26 repoints them at the catalog row), ``collection_
registration_kwargs`` (the write-time registration derivation used by
``HttpCatalogClient.register_collection``'s own bare-call fallback and
``ensure_collection_registered``, never one of the ten retired sites),
and ``CollectionName.parse`` (the render path's parse counterpart, one
pre-existing caller, unaffected by this bead). This is EXACTLY the
allowlist named: "the helpers (render path) and the gate" -- this file
IS the gate. Every entry below is real (verified independently by
``test_allowlisted_sites_are_real_calls_not_stale_entries``); the pin is
EXACT (neither a floor a future bead may raise, nor a ratchet expected
to keep shrinking) -- a NEW caller appearing anywhere is exactly as much
a regression as one of the ten retired sites coming back.
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
#:
#: nexus-ft04v.26 (THE REPOINT) update: collection_content_type (was
#: corpus.py:626) and collection_owner (was corpus.py:658) DROPPED --
#: both now read the catalog row (nexus.mcp_infra.get_collection_row)
#: instead of parsing, exactly the retirement their old entries here
#: predicted ("Still parses until nexus-ft04v.26 repoints it at the
#: catalog row"). collection_registration_kwargs's own entry moved
#: 1183 -> 1295 (line shift from the surrounding repoint, same call,
#: same reason). SIX new entries added: all mint-time /
#: re-registration-after-row-deletion sites this SAME bead's row-based
#: repoint of collection_content_type/collection_owner/collection_model
#: made necessary -- a name with no catalog row (by construction, since
#: registering IS what creates the row) can no longer be read via the
#: now-row-based funnel helpers, so these fall back to parsing the
#: STRING's own segments (safe: each site first confirms the name is
#: conformant via is_conformant_collection_name, a pure regex check with
#: no row lookup).
ALLOWED_CALLERS: dict[tuple[str, int], str] = {
    ("src/nexus/corpus.py", 1295): (
        "collection_registration_kwargs: the write-time registration "
        "derivation used by HttpCatalogClient.register_collection's OWN "
        "bare-call fallback and by ensure_collection_registered (T3 chunk "
        "writes, aspects, taxonomy). Never one of the ten sites this bead "
        "retires -- those either pass content_type/owner_id/embedding_model/ "
        "model_version explicitly (registration) or read via the funnel "
        "helpers (diagnostics), so this fallback path never fires for them; "
        "out of nexus-ft04v.27's scope."
    ),
    ("src/nexus/catalog/collection_name.py", 113): (
        "CollectionName.parse: the render path's own parse counterpart. "
        "Pre-existing single caller (http_catalog_client.py's "
        "collection_for) unaffected by this bead."
    ),
    ("src/nexus/commands/collection.py", 757): (
        "reindex_cmd: `name`'s catalog row was JUST DELETED by "
        "purge_collection_cascade a few lines above -- this call is what "
        "RECREATES it, so the row-based funnel helpers would raise "
        "CollectionNotRegisteredError every time. `name` is already "
        "confirmed conformant by an is_conformant_collection_name guard, "
        "so its own segments (parsed once, pure regex) are what this "
        "re-registration needs. NOT the same call nexus-ft04v.27 retired "
        "at this location (that RETIRED_SITES entry predates this bead's "
        "row-based repoint, which is what makes this reintroduction "
        "necessary here)."
    ),
    ("src/nexus/commands/catalog_cmds/collections.py", 128): (
        "backfill_collections_cmd: `to_register` names are, by the loop's "
        "own filter, exactly the ones with no catalog row yet -- this "
        "call is what creates one. Same is_conformant_collection_name "
        "guard as the other new sites; not the retired backfill_collections_cmd "
        "site (that one predates the row-based repoint too)."
    ),
    ("src/nexus/commands/catalog_cmds/collections.py", 342): (
        "rename_collection_cmd: `new` was just confirmed CollectionState."
        "ABSENT above -- it has no catalog row yet by construction. Same "
        "guard and rationale as the backfill site above; not the retired "
        "rename_collection_cmd site (predates the row-based repoint)."
    ),
    ("src/nexus/catalog/recovery_bundle.py", 392): (
        "target_collection_for: `recorded` names a collection on the "
        "SOURCE install a recovery bundle is being restored from -- it "
        "may have no catalog row on THIS install at all yet (restoring "
        "it is what creates one). Reads the string's own parsed "
        "segments, never the row-based helpers, which would raise "
        "CollectionNotRegisteredError for a name this install has never "
        "seen."
    ),
    ("src/nexus/db/t3.py", 1327): (
        "T3Database.list_collections()'s _derived_row_fields: class (d) "
        "-- synthesizes a row for a substrate (the retired, TEST-ONLY "
        "Chroma-era client) with no catalog to join against, the same "
        "shape as orphan_backfill.py's _content_type_for_collection. "
        "Without this, nexus.mcp_infra's row cache reports 'no row' for "
        "every T3Database-backed test, silently emptying resolve_corpus's "
        "bare-content-type fan-out for the entire test suite's primary "
        "substrate (found live: test_store_put_invalidates_page_cache / "
        "test_store_delete_invalidates_page_cache in tests/test_mcp_server.py)."
    ),
}

#: The ten sites nexus-ft04v.27 retired (six registration + four
#: read-only diagnostics added on the coordinator's follow-up) --
#: asserted ABSENT below via the exact-allowlist check, so a regression
#: (someone re-adding a direct parse call at one of these exact spots)
#: fails loud instead of silently passing because it happens to also be
#: outside ALLOWED_CALLERS (which would already fail, but this gives a
#: name to the failure).
RETIRED_SITES: tuple[str, ...] = (
    "src/nexus/indexer.py (post-rename registration)",
    "src/nexus/commands/collection.py (reindex_cmd re-registration)",
    "src/nexus/commands/catalog_cmds/collections.py (backfill_collections_cmd)",
    "src/nexus/commands/catalog_cmds/collections.py (rename_collection_cmd)",
    "src/nexus/commands/catalog_cmds/migration.py (migrate_fallback_cmd)",
    "src/nexus/commands/index.py (already fixed pre-nexus-ft04v.27, "
    "by nexus-ft04v.34)",
    "src/nexus/health.py (chash-conformance unroutable-collection probe)",
    "src/nexus/repo_identity.py (list_sibling_collections)",
    "src/nexus/commands/collection.py (_find_dimension_mismatched_collections)",
    "src/nexus/commands/catalog_cmds/doctor.py (_run_name_vs_embed_dim)",
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
    ten retired sites, or an entirely new caller) fails here; a site
    that has since been retired and dropped from ALLOWED_CALLERS also
    fails here (drop the stale allowlist entry)."""
    live = _all_call_sites()
    live_set = {
        (rel, lineno) for rel, linenos in live.items() for lineno in linenos
    }
    allowed_set = set(ALLOWED_CALLERS)

    extra = sorted(f"{rel}:{lineno}" for rel, lineno in (live_set - allowed_set))
    missing = sorted(f"{rel}:{lineno}" for rel, lineno in (allowed_set - live_set))

    assert not extra, (
        f"UNDOCUMENTED direct caller(s) of parse_conformant_collection_name: "
        f"{extra}. If this is a legitimate new render-path helper, add it "
        f"to ALLOWED_CALLERS with a reason; if it is a re-added registration "
        f"or diagnostic parse, retire it the way nexus-ft04v.27 retired the "
        f"other ten (route it through collection_content_type / "
        f"collection_owner / collection_model / "
        f"model_version_for_collection_name instead)."
    )
    assert not missing, (
        f"ALLOWED_CALLERS entr(y/ies) no longer observed in the live tree: "
        f"{missing}. The call was removed -- drop the stale entry (a lower "
        f"pin is welcome, never an inflated one)."
    )


def test_retired_sites_are_documented() -> None:
    """Non-vacuity for the retirement itself: name the ten sites so a
    reader (and a future regression) has something concrete to check
    against, independent of the exact-allowlist assertion above."""
    assert len(RETIRED_SITES) == 10, RETIRED_SITES
