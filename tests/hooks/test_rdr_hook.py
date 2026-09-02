# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the RDR SessionStart hook.

RDR-103 Phase 3b + Phase 5: ``rdr_hook.py`` resolves the indexed
collection name through the catalog (via
``Catalog.collection_for_repo``) when both the catalog and the owner
exist. Without a catalog or owner row, the helper falls back to
:func:`nexus.indexer._repo_collection_or_legacy` which synthesises a
conformant 4-segment name from the path-derived identity (Phase 5
tightening; pre-Phase-5 the fallback was the legacy 2-segment shape).
The test surface pins:

  - The helper synthesises a conformant 4-segment name when no catalog
    resolution is available. (nexus-i711w terminal deletion: the
    local-catalog resolution leg and its two pins retired with the local
    catalog — see the tombstones below.)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "conexus" / "hooks" / "scripts" / "rdr_hook.py"


@pytest.fixture()
def rdr_hook_module():
    """Import ``rdr_hook.py`` as a module so we can call its helpers
    directly. The script is not on the import path by default — load
    it via spec_from_file_location."""
    spec = importlib.util.spec_from_file_location("rdr_hook_under_test", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# test_resolve_rdr_collection_uses_catalog_when_initialized (and its
# catalog_with_owner fixture) RETIRED (nexus-i711w terminal deletion): its
# subject was the hook's LOCAL-``Catalog.init`` resolution leg, which is dead
# code now (the hook's ``from nexus.catalog import Catalog`` raises and it
# always falls through to the indexer's path-derived conformant synthesis —
# the behaviour pinned by the surviving tests below).


def test_resolve_rdr_collection_synthesises_conformant_when_catalog_absent(
    rdr_hook_module, tmp_path, monkeypatch,
):
    """No catalog at the configured path: helper falls back to the
    indexer's path-derived conformant synthesis. Keeps SessionStart
    functional on workstations that have not initialized the catalog
    while still emitting a 4-segment name that satisfies T3's
    strict-naming guard (RDR-103 Phase 5).
    """
    repo = tmp_path / "isolated"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: tmp_path / "no_such_catalog",
    )
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("isolated", "abcdef12"),
    )
    name = rdr_hook_module._resolve_rdr_collection(repo)
    assert name == "rdr__isolated-abcdef12__voyage-context-3__v1"


# test_resolve_rdr_collection_synthesises_conformant_when_owner_unregistered
# RETIRED (nexus-i711w terminal deletion): its premise — a LOCAL catalog
# initialized via ``Catalog.init`` with no owner row — died with the local
# catalog. The surviving fallback contract (helper synthesises the conformant
# 4-segment name when no catalog resolution is available) is pinned by
# test_resolve_rdr_collection_synthesises_conformant_when_catalog_absent above.


# ── nexus-e2sim: `open` is a pre-accept synonym for `draft` (GH #1409) ──────


def _write_rdr(tmp_path: Path, status: str) -> Path:
    f = tmp_path / "001-test-decision.md"
    f.write_text(f"---\nstatus: {status}\ntitle: test\n---\n\n# RDR-001\n")
    return f


def test_reconcile_open_file_vs_draft_t2_is_no_op(
    rdr_hook_module, tmp_path, monkeypatch
) -> None:
    """nexus-e2sim (GH #1409 follow-through): rdr-create seeds T2 at
    'draft', so an RDR file legitimately using 'open' (the qsryj-accepted
    pre-accept synonym) must NOT be silently rewritten back to 'draft' by
    the SessionStart reconcile — the exact revert the fix was filed
    against. Equal rank means neither side wins: pure no-op."""
    mod = rdr_hook_module
    f = _write_rdr(tmp_path, "open")
    file_writes: list = []
    t2_writes: list = []
    monkeypatch.setattr(
        mod, "_update_file_status", lambda *a: file_writes.append(a) or True
    )
    monkeypatch.setattr(
        mod, "_update_t2_status", lambda *a: t2_writes.append(a) or True
    )

    reconciled = mod._reconcile(
        tmp_path, "myrepo", [f], {"001": "draft"}
    )

    assert reconciled == 0
    assert file_writes == [], (
        "the hook must not rewrite an 'open' file back to 'draft' "
        "(the GH #1409 revert this test pins)"
    )
    assert t2_writes == []


def test_reconcile_draft_file_vs_open_t2_is_no_op(
    rdr_hook_module, tmp_path, monkeypatch
) -> None:
    mod = rdr_hook_module
    f = _write_rdr(tmp_path, "draft")
    file_writes: list = []
    t2_writes: list = []
    monkeypatch.setattr(
        mod, "_update_file_status", lambda *a: file_writes.append(a) or True
    )
    monkeypatch.setattr(
        mod, "_update_t2_status", lambda *a: t2_writes.append(a) or True
    )

    reconciled = mod._reconcile(
        tmp_path, "myrepo", [f], {"001": "open"}
    )

    assert reconciled == 0
    assert file_writes == [] and t2_writes == []


def test_reconcile_open_file_still_advances_to_accepted_t2(
    rdr_hook_module, tmp_path, monkeypatch
) -> None:
    """'open' ranks WITH 'draft', not above the lifecycle: a T2 record at
    'accepted' still wins and updates the file, same as it would for
    'draft'."""
    mod = rdr_hook_module
    f = _write_rdr(tmp_path, "open")
    file_writes: list = []
    monkeypatch.setattr(
        mod, "_update_file_status", lambda *a: file_writes.append(a) or True
    )
    monkeypatch.setattr(mod, "_update_t2_status", lambda *a: True)

    reconciled = mod._reconcile(
        tmp_path, "myrepo", [f], {"001": "accepted"}
    )

    assert reconciled == 1
    assert file_writes == [(f, "accepted")]


# ── RDR-201 P1.5 (nexus-j9z30.5): _STATUS_ORDER/_TERMINAL derive from the
# packaged lifecycle table, not a hand-maintained literal ─────────────────

PACKAGE_TABLE_PATH = REPO_ROOT / "src" / "nexus" / "tables" / "rdr-lifecycle.toml"
PLUGIN_TABLE_PATH = REPO_ROOT / "conexus" / "resources" / "tables" / "rdr-lifecycle.toml"


def test_plugin_lifecycle_table_byte_identical_to_package_copy():
    """The hook runs under bare system python (no ``nexus`` import — see
    ``rdr_hook.py``'s module docstring / bead nexus-j9z30.5), so it cannot
    reach ``nexus.tables.load.load_packaged_table``. It reads its OWN copy
    shipped at ``conexus/resources/tables/``; this byte-identity check is
    the drift tripwire, same pattern as
    ``TestPluginWiring::test_shellib_parity_with_reference`` in
    ``tests/hooks/test_subagent_stop_hook.py``."""
    assert PACKAGE_TABLE_PATH.exists(), "package lifecycle table missing"
    assert PLUGIN_TABLE_PATH.exists(), "plugin copy of rdr-lifecycle.toml missing"
    assert PLUGIN_TABLE_PATH.read_bytes() == PACKAGE_TABLE_PATH.read_bytes(), (
        "conexus/resources/tables/rdr-lifecycle.toml has drifted from "
        "src/nexus/tables/rdr-lifecycle.toml — edit the package copy, then "
        "copy it over"
    )


def test_exclude_files_covers_agents_md(rdr_hook_module) -> None:
    assert "agents.md" in rdr_hook_module._EXCLUDE_FILES


_FAKE_LIFECYCLE_DOC = {
    "lifecycle": {"terminal_preserving_events": ["supersede"]},
    "dimensions": {"status": {"domain": ["a", "b", "c", "d"]}},
    "row": [
        # a -[advance]-> b : non-terminal outgoing from a
        {"id": "advance", "match": {"status": "a", "event": "advance"}, "to": {"status": "b"}},
        # b -[advance]-> c : non-terminal outgoing from b
        {"id": "advance2", "match": {"status": "b", "event": "advance"}, "to": {"status": "c"}},
        # c's ONLY non-refuse outgoing row is a 'supersede' event -> excluded
        # from the non-terminal computation -> c is terminal.
        {"id": "supersede", "match": {"status": "c", "event": "supersede"}, "to": {"status": "d"}},
        # d has no outgoing 'to' row at all -> terminal.
    ],
}


def test_derive_status_order_and_terminal_ranks_non_terminal_in_domain_order(
    rdr_hook_module,
) -> None:
    order, terminal = rdr_hook_module._derive_status_order_and_terminal(_FAKE_LIFECYCLE_DOC)
    assert order["a"] == 0
    assert order["b"] == 1
    assert terminal == {"c", "d"}
    assert order["c"] == order["d"] == 2


def test_derive_status_order_and_terminal_ranks_open_with_draft(rdr_hook_module) -> None:
    doc = {
        "dimensions": {"status": {"domain": ["draft", "accepted"]}},
        "row": [
            {"id": "accept", "match": {"status": "draft", "event": "accept"},
             "to": {"status": "accepted"}},
        ],
    }
    order, terminal = rdr_hook_module._derive_status_order_and_terminal(doc)
    assert order["draft"] == 0
    assert order["open"] == order["draft"]
    assert terminal == {"accepted"}


def test_derive_status_order_and_terminal_excludes_supersede_outgoing(rdr_hook_module) -> None:
    """A status whose ONLY non-refuse outgoing row is a ``supersede`` event
    is still terminal -- 'can still be superseded' does not make a status
    non-terminal (matches the real table's closed/superseded/abandoned)."""
    doc = {
        "lifecycle": {"terminal_preserving_events": ["supersede"]},
        "dimensions": {"status": {"domain": ["draft", "closed"]}},
        "row": [
            {"id": "accept", "match": {"status": "draft", "event": "accept"},
             "to": {"status": "closed"}},
            {"id": "supersede", "match": {"status": "closed", "event": "supersede"},
             "to": {"status": "closed"}},
        ],
    }
    order, terminal = rdr_hook_module._derive_status_order_and_terminal(doc)
    assert terminal == {"closed"}


def test_derive_status_order_and_terminal_reads_configured_event_name_not_hardcoded(
    rdr_hook_module,
) -> None:
    """RDR-201 P1.5 fix round (T2 nexus/critique-nexus-j9z30-5-2026-09-01
    [24042] finding 3): the terminal-preserving event set comes from the
    table's own ``[lifecycle] terminal_preserving_events`` list, not a
    hardcoded ``== "supersede"`` in this hook. An event named ``retire``
    (never ``supersede``) declared as terminal-preserving must behave
    identically to the supersede case above -- proving the rule is
    data-driven, not a literal string match."""
    doc = {
        "lifecycle": {"terminal_preserving_events": ["retire"]},
        "dimensions": {"status": {"domain": ["draft", "closed"]}},
        "row": [
            {"id": "accept", "match": {"status": "draft", "event": "accept"},
             "to": {"status": "closed"}},
            {"id": "retire", "match": {"status": "closed", "event": "retire"},
             "to": {"status": "closed"}},
        ],
    }
    order, terminal = rdr_hook_module._derive_status_order_and_terminal(doc)
    assert terminal == {"closed"}


def test_derive_status_order_and_terminal_no_lifecycle_section_excludes_nothing(
    rdr_hook_module,
) -> None:
    """No ``[lifecycle]`` section -> an empty terminal-preserving-events
    set (explicit, documented default), NOT a silent fallback to
    ``supersede``. A status whose only outgoing row is a ``supersede``
    event is therefore NON-terminal here, unlike the two tests above where
    the table explicitly declares the event."""
    doc = {
        "dimensions": {"status": {"domain": ["draft", "closed"]}},
        "row": [
            {"id": "accept", "match": {"status": "draft", "event": "accept"},
             "to": {"status": "closed"}},
            {"id": "supersede", "match": {"status": "closed", "event": "supersede"},
             "to": {"status": "closed"}},
        ],
    }
    order, terminal = rdr_hook_module._derive_status_order_and_terminal(doc)
    assert terminal == set()


def test_derive_status_order_and_terminal_on_real_table_matches_established_ranks(
    rdr_hook_module,
) -> None:
    """The real rdr-lifecycle table's derived order/terminal set must
    preserve the pre-P1.5 behavior this hook's own reconcile tests above
    depend on: open ranks with draft (rank 0), accepted advances past it,
    and closed/superseded/abandoned are all terminal."""
    import tomllib

    doc = tomllib.loads(PLUGIN_TABLE_PATH.read_text(encoding="utf-8"))
    order, terminal = rdr_hook_module._derive_status_order_and_terminal(doc)
    assert order["draft"] == 0
    assert order["open"] == 0
    assert order["accepted"] == 1
    assert order["deferred"] == 2
    assert terminal == {"closed", "superseded", "abandoned"}
    terminal_rank = order["closed"]
    assert order["superseded"] == terminal_rank
    assert order["abandoned"] == terminal_rank
    assert terminal_rank > order["deferred"] > order["accepted"] > order["draft"]


def test_module_level_status_order_and_terminal_loaded_from_plugin_table(
    rdr_hook_module,
) -> None:
    """The module-level ``_STATUS_ORDER``/``_TERMINAL`` the hook actually
    uses at runtime are populated from the real plugin table, not left at
    an empty defensive-fallback default."""
    assert rdr_hook_module._STATUS_ORDER["draft"] == 0
    assert rdr_hook_module._STATUS_ORDER["open"] == 0
    assert rdr_hook_module._TERMINAL == {"closed", "superseded", "abandoned"}


def test_real_table_declares_terminal_preserving_events_section():
    """The package table (and, by the byte-identity test above, the plugin
    copy) must carry the ``[lifecycle] terminal_preserving_events`` section
    the hook now reads instead of hardcoding ``"supersede"``."""
    import tomllib

    doc = tomllib.loads(PACKAGE_TABLE_PATH.read_text(encoding="utf-8"))
    assert doc.get("lifecycle", {}).get("terminal_preserving_events") == ["supersede"]


def test_real_table_header_states_the_terminal_rule():
    """RDR-201 P1.5 fix round (T2 nexus/critique-nexus-j9z30-5-2026-09-01
    [24042] finding 2): the downstream terminal-derivation assumption is
    named in the table's own header comment, not only in rdr_hook.py --
    a future table author adding a second such event has somewhere to
    read the rule from."""
    header = PACKAGE_TABLE_PATH.read_text(encoding="utf-8").split("[table]")[0]
    assert "terminal" in header.lower()
    assert "terminal_preserving_events" in header


def test_real_table_declares_a_version():
    import tomllib

    doc = tomllib.loads(PACKAGE_TABLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(doc["table"].get("version"), int)


def test_load_status_order_and_terminal_logs_table_id_and_version(
    rdr_hook_module, tmp_path, monkeypatch, capsys
) -> None:
    """RDR-201 P1.5 fix round (T2 nexus/critique-nexus-j9z30-5-2026-09-01
    [24042] finding 4): a successful load logs the loaded table's id +
    version on stderr, so plugin/package table skew (a client upgraded
    ahead of the pinned plugin, or vice versa -- RDR-143's problem
    statement) is at least DETECTABLE from this line. No lockstep
    mechanism is introduced here -- that is RDR-143's scope."""
    fake_table_path = tmp_path / "versioned-lifecycle.toml"
    fake_table_path.write_text(
        '[table]\nid = "rdr-lifecycle"\nkind = "state-machine"\nversion = 7\n\n'
        '[dimensions.status]\ndomain = ["draft"]\n'
    )
    monkeypatch.setattr(rdr_hook_module, "_LIFECYCLE_TABLE_PATH", fake_table_path)

    rdr_hook_module._load_status_order_and_terminal()

    captured = capsys.readouterr()
    assert "rdr-lifecycle" in captured.err
    assert "7" in captured.err
