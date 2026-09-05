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
import re
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


# ── nexus-e19sa (Sam's ruling, 2026-09-02): the reconcile half is gone,
# the summary must actually print ──────────────────────────────────────────
#
# The file filter used to be ``re.match(r"\d+", p.stem)`` against stems shaped
# ``rdr-201-...``: zero matches, ``sys.exit(0)`` before any logic, on every
# session since the hook was written. Nobody noticed the silence. The
# reconcile tests that lived here exercised ``_reconcile`` directly and so
# never saw that ``main`` could not reach it; they went with the code they
# pinned. What replaces them pins the two things that matter now: the filter
# selects this repo's real files, and the summary prints for the real tree.

PACKAGE_TABLE_PATH = REPO_ROOT / "src" / "nexus" / "tables" / "rdr-lifecycle.toml"


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("rdr-201-closed-vocabularies-as-checked-tables", "201"),
        ("rdr137-test-fixture-partition-deliverable", "137"),
        ("001-legacy-shape", "001"),
        ("RDR-005-upper-case", "005"),
        ("status-census-2026-09-01", None),
        ("README", None),
        ("rdr-", None),
    ],
)
def test_extract_rdr_id_matches_this_repos_filename_shapes(rdr_hook_module, stem, expected) -> None:
    assert rdr_hook_module._extract_rdr_id(Path(f"/x/{stem}.md")) == expected


def test_rdr_files_selects_every_rdr_in_a_fixture_tree_and_nothing_else(
    rdr_hook_module, tmp_path,
) -> None:
    rdr_dir = tmp_path / "docs" / "rdr"
    (rdr_dir / "post-mortem").mkdir(parents=True)
    for name in (
        "rdr-201-thing.md", "rdr137-legacy.md", "001-older.md",
        "README.md", "AGENTS.md", "template.md", "status-census-2026-09-01.md",
    ):
        (rdr_dir / name).write_text("---\nstatus: draft\n---\n")
    (rdr_dir / "post-mortem" / "rdr-191-postmortem.md").write_text("---\nstatus: closed\n---\n")

    selected = sorted(p.name for p in rdr_hook_module._rdr_files(rdr_dir))
    assert selected == ["001-older.md", "rdr-201-thing.md", "rdr137-legacy.md"]


def test_rdr_files_on_the_real_tree_is_not_vacuous(rdr_hook_module) -> None:
    """The filter that selected nothing for the life of this hook must select
    this repo's own RDRs (nexus-moht0 vacuous-gate doctrine: a sweep that
    examined nothing is a failure, not a pass)."""
    files = rdr_hook_module._rdr_files(REPO_ROOT / "docs" / "rdr")
    assert len(files) > 200, len(files)
    assert all(rdr_hook_module._extract_rdr_id(p) for p in files)
    assert "AGENTS.md" not in {p.name for p in files}


def test_summary_prints_for_this_repos_real_tree(rdr_hook_module, monkeypatch, capsys) -> None:
    """The ruling's own test: run ``main`` against THIS checkout's real
    ``docs/rdr/`` tree, with only the substrate calls (T2, catalog, the
    ``nx collection list`` subprocess) stubbed, and require the summary
    line to PRINT with a document count that could only come from the
    real files. Silence is the failure this hook shipped with."""
    mod = rdr_hook_module
    monkeypatch.setattr(mod, "_repo_root", lambda: REPO_ROOT)
    monkeypatch.setattr(mod, "_load_all_t2_statuses", lambda repo: {"1": "closed", "2": "closed", "3": "accepted"})
    monkeypatch.setattr(mod, "_resolve_rdr_collection", lambda root: "rdr__nexus-1-1__voyage-context-3__v1")
    monkeypatch.setattr(mod, "_collection_exists", lambda target: False)

    with pytest.raises(SystemExit) as excinfo:
        mod.main()
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    m = re.search(r"^RDR: (\d+) documents \(2 closed, 1 accepted\) in docs/rdr but NOT indexed\.$", out, re.M)
    assert m, out
    assert int(m.group(1)) > 200, out
    assert "Run: nx index repo" in out
    # nexus-3o4lt: the old remedy minted curator-owner rows with absolute
    # paths for every RDR; the hook must never recommend it again.
    assert "nx index rdr" not in out


def test_hook_carries_no_writer(rdr_hook_module) -> None:
    """Ruling nexus-e19sa: the reconcile half is deleted, not disabled. A
    future re-introduction has to argue with this."""
    for name in ("_reconcile", "_update_file_status", "_update_t2_status",
                 "_STATUS_ORDER", "_TERMINAL", "_derive_status_order_and_terminal"):
        assert not hasattr(rdr_hook_module, name), name


def test_exclude_files_covers_agents_md(rdr_hook_module) -> None:
    assert "agents.md" in rdr_hook_module._EXCLUDE_FILES


def test_real_table_declares_a_version():
    import tomllib

    doc = tomllib.loads(PACKAGE_TABLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(doc["table"].get("version"), int)
