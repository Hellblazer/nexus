# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog manifest-verify TUMBLER`` (nexus-5xn3k.6, design memo §4).

NOT ``nx catalog verify`` — that is a pre-existing, unrelated command
(nexus-whh61.4, ``catalog_cmds/integrity.py``: corpus-wide ghost-tumbler
sweep + ``--heal``). This bead's verb collided with that name on first
landing (both registered `@catalog.command("verify")` onto the same click
group, silently shadowing one) and was renamed to ``manifest-verify`` —
see ``TestCatalogVerifyCmd.test_does_not_collide_with_ghost_verify`` below.

Unit-level, monkeypatching ``nexus.commands.catalog._get_catalog`` with a
lightweight fake reader — the same style as ``show``/``search`` don't need a
live service catalog to exercise the CLI surface (argument parsing, output
formatting, error mapping). Covers:

* Happy path: clean manifest (missing=0) renders OK, plain text and --json.
* Damaged path: missing>0 renders a DAMAGED verdict + repair hint.
* Not-found tumbler exits non-zero with a clear message.
* manifest_verify() failure PROPAGATES as a ClickException (never a
  false-clean skip — this is a diagnostic verb, not a background check).
* READ-ONLY: the command never reaches for a catalog writer at all — the
  fixture below fails the test outright if ``_get_catalog_writer`` is
  invoked, which is a stronger guarantee than merely asserting no writer
  METHOD was called on an already-open writer.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.catalog.tumbler import Tumbler
from nexus.commands.catalog import catalog as catalog_group

# RDR-109 Phase 2 convention (matches test_catalog_cli.py / test_commands_dt.py):
# cloud-mode canonical behavior so assertions don't depend on the host env.
pytestmark = pytest.mark.usefixtures("cloud_mode")

TUMBLER = Tumbler.parse("1.1.1")


class _Entry:
    def __init__(self, tumbler, title="Test Doc", index_state="complete",
                 index_content_hash="deadbeef", file_path="src/nexus/foo.py"):
        self.tumbler = tumbler
        self.title = title
        self.index_state = index_state
        self.index_content_hash = index_content_hash
        self.file_path = file_path


class _FakeCat:
    def __init__(
        self, entry=None, verify_result=None, verify_exc=None,
        verify_all_result=None, verify_all_exc=None,
        orphans_by_dim=None, orphans_exc=None,
    ):
        self._entry = entry
        self._verify_result = verify_result
        self._verify_exc = verify_exc
        self._verify_all_result = verify_all_result
        self._verify_all_exc = verify_all_exc
        self._orphans_by_dim = orphans_by_dim or {}
        self._orphans_exc = orphans_exc
        self.verify_calls: list[str] = []

    def resolve(self, t):
        return self._entry if self._entry is not None and str(t) == str(self._entry.tumbler) else None

    def find(self, query):
        return []

    def manifest_verify(self, doc_id: str) -> dict:
        self.verify_calls.append(doc_id)
        if self._verify_exc is not None:
            raise self._verify_exc
        return self._verify_result or {}

    def manifest_verify_all(self) -> dict:
        if self._verify_all_exc is not None:
            raise self._verify_all_exc
        return self._verify_all_result or {}

    def manifest_orphans(self, dim: int, *, limit: int = 100) -> dict:
        if self._orphans_exc is not None:
            raise self._orphans_exc
        rows = self._orphans_by_dim.get(dim, [])
        return {"dim": dim, "count": len(rows), "orphans": rows[:limit]}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def no_writer(monkeypatch):
    """READ-ONLY guarantee: fail loudly if `verify` ever reaches for a
    catalog writer — a write proxy touched at all (even unused) would be
    the regression this fixture exists to catch."""
    def _boom(*a, **kw):
        raise AssertionError(
            "nx catalog manifest-verify must be read-only — "
            "_get_catalog_writer must never be called"
        )
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", _boom)


class TestCatalogVerifyCmd:
    def test_clean_manifest_renders_ok(self, runner, monkeypatch):
        cat = _FakeCat(
            _Entry(TUMBLER),
            verify_result={"referenced": 5, "present": 5, "missing": 0},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1"])
        assert result.exit_code == 0, result.output
        assert "Referenced:  5" in result.output
        assert "Present:     5" in result.output
        assert "Missing:     0" in result.output
        assert "OK" in result.output
        assert "DAMAGED" not in result.output
        assert cat.verify_calls == ["1.1.1"]

    def test_damaged_manifest_reports_missing_and_repair_hint(self, runner, monkeypatch, tmp_path):
        """nexus-sj4a3: DAMAGED now exits 1, and a doc whose file_path
        resolves to a real, existing file gets the `nx index <path>
        --force` hint (with the real path, not a literal placeholder)."""
        real_file = tmp_path / "thing.py"
        real_file.write_text("# real file on disk\n")
        cat = _FakeCat(
            _Entry(TUMBLER, file_path=str(real_file)),
            verify_result={"referenced": 10, "present": 7, "missing": 3},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1"])
        assert result.exit_code == 1, result.output
        assert "Missing:     3" in result.output
        assert "DAMAGED" in result.output
        assert "--force" in result.output
        assert str(real_file) in result.output

    def test_damaged_manifest_without_file_path_gets_store_put_hint(self, runner, monkeypatch):
        """A document with no indexable file_path (e.g. a `store put`-origin
        note) must not print the misleading `nx index <path> --force` hint —
        there is no path to re-index."""
        cat = _FakeCat(
            _Entry(TUMBLER, file_path=""),
            verify_result={"referenced": 4, "present": 3, "missing": 1},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1"])
        assert result.exit_code == 1, result.output
        assert "DAMAGED" in result.output
        assert "--force" not in result.output
        assert "reconcile" in result.output or "store put" in result.output

    def test_damaged_manifest_with_stale_file_path_gets_reconcile_hint(
        self, runner, monkeypatch, tmp_path,
    ):
        """nexus-sj4a3 code-review SUGGESTION: a file_path that no longer
        exists on disk (moved/deleted since indexing) is exactly as
        unusable as no file_path at all for `nx index <path> --force` — the
        old check was truthiness-only (`if entry.file_path:`) and printed
        an actionable-looking but broken hint for a stale path."""
        missing_path = str(tmp_path / "does_not_exist_anymore.py")
        cat = _FakeCat(
            _Entry(TUMBLER, file_path=missing_path),
            verify_result={"referenced": 4, "present": 3, "missing": 1},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1"])
        assert result.exit_code == 1, result.output
        assert "DAMAGED" in result.output
        assert "--force" not in result.output
        assert "reconcile" in result.output or "store put" in result.output

    def test_json_output_shape(self, runner, monkeypatch):
        cat = _FakeCat(
            _Entry(TUMBLER, index_state="indexing"),
            verify_result={"referenced": 2, "present": 1, "missing": 1},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload == {
            "tumbler": "1.1.1",
            "referenced": 2,
            "present": 1,
            "missing": 1,
            "index_state": "indexing",
            "index_content_hash": "deadbeef",
            "damaged": True,
        }

    def test_not_found_exits_nonzero(self, runner, monkeypatch):
        cat = _FakeCat(_Entry(TUMBLER))
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "9.9.9"])
        assert result.exit_code != 0
        assert "Not found" in result.output

    def test_manifest_verify_failure_propagates_never_false_clean(self, runner, monkeypatch):
        """Falsifies a fail-open regression: if a future edit swallows the
        exception and renders a clean result instead, this test catches it."""
        cat = _FakeCat(_Entry(TUMBLER), verify_exc=RuntimeError("engine unreachable"))
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1"])
        assert result.exit_code != 0
        assert "manifest_verify failed" in result.output
        assert "engine unreachable" in result.output
        assert "OK" not in result.output

    def test_does_not_collide_with_ghost_verify(self):
        """Regression pin: this bead's verb first landed as
        ``@catalog.command("verify")``, silently shadowing the pre-existing
        nexus-whh61.4 ghost-tumbler sweep registered under the SAME name on
        the SAME click group (both `catalog.command("verify")` — Click keeps
        only the last registration, so `nx catalog verify` ran whichever
        module happened to import last, and `tests/test_catalog_cli.py::
        TestVerifyCommand` went red with no code changes on its side).
        Renamed to `manifest-verify`. Both must be independently reachable."""
        assert "verify" in catalog_group.commands
        assert "manifest-verify" in catalog_group.commands
        assert catalog_group.commands["verify"] is not catalog_group.commands["manifest-verify"]


# ── --list (nexus-heizf part 1) ─────────────────────────────────────────────


class TestManifestVerifyListMode:
    def test_list_rejects_a_positional_tumbler(self, runner, monkeypatch):
        cat = _FakeCat()
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "1.1.1", "--list"])
        assert result.exit_code != 0
        assert "no TUMBLER_OR_TITLE" in result.output or "no TUMBLER_OR_TITLE" in str(result.exception)

    def test_missing_tumbler_without_list_is_a_usage_error(self, runner, monkeypatch):
        cat = _FakeCat()
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify"])
        assert result.exit_code != 0

    def test_clean_catalog_exits_zero(self, runner, monkeypatch):
        cat = _FakeCat(verify_all_result={
            "collections": [{"collection": "code__x__bge-base-en-v15-768__v1",
                              "referenced": 5, "present": 5, "missing": 0}],
            "count": 1,
        })
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_clean_catalog_json(self, runner, monkeypatch):
        cat = _FakeCat(verify_all_result={"collections": [], "count": 0})
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        population = payload.pop("population")
        assert payload == {
            "collections": [], "total_rows": 0,
            "unroutable_collections": [], "incomplete_collections": {},
            "clean": True,
        }
        assert "purge-trash" in population
        assert "stranded" in population

    def test_happy_path_enumerates_grouped_by_doc(self, runner, monkeypatch):
        coll = "code__x__bge-base-en-v15-768__v1"
        cat = _FakeCat(
            verify_all_result={
                "collections": [{"collection": coll, "referenced": 2, "present": 0, "missing": 2}],
                "count": 1,
            },
            orphans_by_dim={768: [
                {"tenant_id": "t", "doc_id": "1.2.3", "position": 0, "chash": "a" * 64, "collection": coll},
                {"tenant_id": "t", "doc_id": "1.2.3", "position": 1, "chash": "b" * 64, "collection": coll},
            ]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code == 1, result.output
        assert coll in result.output
        assert "1.2.3" in result.output
        assert "positions=[0-1]" in result.output
        assert "distinct_chashes=2" in result.output

    def test_happy_path_json_shape(self, runner, monkeypatch):
        coll = "code__x__bge-base-en-v15-768__v1"
        cat = _FakeCat(
            verify_all_result={
                "collections": [{"collection": coll, "referenced": 1, "present": 0, "missing": 1}],
                "count": 1,
            },
            orphans_by_dim={768: [
                {"tenant_id": "t", "doc_id": "1.2.3", "position": 0, "chash": "a" * 64, "collection": coll},
            ]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["clean"] is False
        assert payload["total_rows"] == 1
        assert payload["unroutable_collections"] == []
        assert payload["collections"] == [{
            "collection": coll,
            "documents": [{"doc_id": "1.2.3", "positions": "0", "distinct_chashes": 1}],
            "row_count": 1,
        }]

    def test_unroutable_collection_surfaces_in_json(self, runner, monkeypatch):
        coll = "code__x__some-future-model-9000__v1"
        cat = _FakeCat(verify_all_result={
            "collections": [{"collection": coll, "referenced": 1, "present": 0, "missing": 1}],
            "count": 1,
        })
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["unroutable_collections"] == [coll]

    def test_text_mode_states_unroutable_count_when_zero_rows_enumerated(
        self, runner, monkeypatch,
    ):
        """nexus-heizf code-review fix round (item 7): with every damaged
        collection unroutable, total_rows is 0 — the summary line must NOT
        read as a bare, contradictory '0 dangling manifest row(s)... ' next
        to a non-zero exit; it must state the unroutable count inline."""
        coll = "code__x__some-future-model-9000__v1"
        cat = _FakeCat(verify_all_result={
            "collections": [{"collection": coll, "referenced": 1, "present": 0, "missing": 1}],
            "count": 1,
        })
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code == 1, result.output
        first_line = [ln for ln in result.output.splitlines() if "dangling manifest row" in ln][0]
        assert "could not be enumerated" in first_line, first_line

    def test_text_mode_carries_population_note(self, runner, monkeypatch):
        coll = "code__x__bge-base-en-v15-768__v1"
        cat = _FakeCat(
            verify_all_result={
                "collections": [{"collection": coll, "referenced": 1, "present": 0, "missing": 1}],
                "count": 1,
            },
            orphans_by_dim={768: [
                {"tenant_id": "t", "doc_id": "1.2.3", "position": 0, "chash": "a" * 64, "collection": coll},
            ]},
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code == 1, result.output
        assert "purge-trash" in result.output
        assert "stranded" in result.output

    def test_incomplete_collections_surface_in_json_and_text(self, runner, monkeypatch):
        """A collection whose enumeration fell short of the census's own
        `missing` count (row cap or a refetch error) must be reported —
        never silently presented as a complete result."""
        coll = "code__x__bge-base-en-v15-768__v1"

        class _PartialCat(_FakeCat):
            def manifest_orphans(self, dim: int, *, limit: int = 100) -> dict:
                # First call: census says 5, only 1 row ever comes back —
                # and it comes back the SAME regardless of `limit`, so the
                # refetch cannot close the gap (an engine-side truncation).
                return {"dim": dim, "count": 5, "orphans": [
                    {"tenant_id": "t", "doc_id": "1.2.3", "position": 0,
                     "chash": "a" * 64, "collection": coll},
                ]}

        cat = _PartialCat(verify_all_result={
            "collections": [{"collection": coll, "referenced": 5, "present": 0, "missing": 5}],
            "count": 1,
        })
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)

        json_result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert json_result.exit_code == 1, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["incomplete_collections"] == {coll: {"enumerated": 1, "expected": 5}}

        text_result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert text_result.exit_code == 1, text_result.output
        assert "PARTIALLY enumerated" in text_result.output
        assert "1 of 5" in text_result.output

    def test_manifest_orphan_report_failure_raises_click_exception(self, runner, monkeypatch):
        """nexus-heizf code-review fix round (item 4): manifest_orphan_report
        catches per-dim manifest_orphans() failures internally (folded into
        unroutable_collections), so this test drives the OUTER guard
        directly — a failure IN manifest_orphan_report itself (e.g. a bug
        in the row-grouping/routing logic, not the network call) must
        surface as a clean ClickException, the same idiom every other
        engine call in this file uses, never a raw traceback."""
        coll = "code__x__bge-base-en-v15-768__v1"
        cat = _FakeCat(verify_all_result={
            "collections": [{"collection": coll, "referenced": 1, "present": 0, "missing": 1}],
            "count": 1,
        })
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        monkeypatch.setattr(
            "nexus.health.manifest_orphan_report",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unexpected internal failure")),
        )
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or "manifest_orphan_report failed" in result.output
        assert "manifest_orphan_report failed" in result.output
        assert "unexpected internal failure" in result.output

    def test_verify_all_failure_propagates_never_false_clean(self, runner, monkeypatch):
        cat = _FakeCat(verify_all_exc=RuntimeError("engine unreachable"))
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code != 0
        assert "manifest_verify_all failed" in result.output
        assert "engine unreachable" in result.output
        assert "OK" not in result.output
