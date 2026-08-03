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
    def __init__(self, entry, verify_result=None, verify_exc=None):
        self._entry = entry
        self._verify_result = verify_result
        self._verify_exc = verify_exc
        self.verify_calls: list[str] = []

    def resolve(self, t):
        return self._entry if str(t) == str(self._entry.tumbler) else None

    def find(self, query):
        return []

    def manifest_verify(self, doc_id: str) -> dict:
        self.verify_calls.append(doc_id)
        if self._verify_exc is not None:
            raise self._verify_exc
        return self._verify_result or {}


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
