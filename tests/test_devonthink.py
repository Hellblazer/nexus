# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nexus.devonthink`` — selector helpers behind ``nx dt`` (RDR-099 P1).

Coverage:

* ``DTNotAvailableError`` is a ``RuntimeError`` subclass.
* ``_run_osascript``: happy path returns stdout; ``subprocess.TimeoutExpired``
  propagates unchanged; non-zero exit with "Application isn't running" stderr
  raises ``DTNotAvailableError``.
* Platform gate: every public selector raises ``DTNotAvailableError`` on
  non-darwin (no osascript spawn).
* ``_dt_selection``: canned-stdout parsing, empty selection, sdef-canonical
  ``selected records`` token in script.
* ``_dt_uuid_record``: keyword ``dt_resolver`` injection; resolver returning
  ``None`` yields ``[]``; default resolver fall-through reuses
  ``aspect_readers._devonthink_resolver_default``.
* ``_dt_tag_records``: empty tag short-circuits (no spawn); multi-database
  iteration when ``database is None``; single-DB scoping when named; UUID
  dedupe across databases.
* ``_dt_group_records``: multi-database iteration; ``/Trash`` and ``/Tags``
  are valid root names.
* ``_dt_smart_group_records``: three-property read (``search predicates``
  PLURAL + ``search group`` + ``exclude subgroups``); ``missing value``
  scope falls through; single-DB scoping.

All tests run unconditionally on Linux/CI by patching ``subprocess.run`` or
``nexus.devonthink._run_osascript`` and ``monkeypatch.setattr("sys.platform", ...)``.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest


# ── DTNotAvailableError ──────────────────────────────────────────────────────


class TestDTNotAvailableError:
    def test_is_runtime_error_subclass(self):
        from nexus.devonthink import DTNotAvailableError

        assert issubclass(DTNotAvailableError, RuntimeError)

    def test_carries_message(self):
        from nexus.devonthink import DTNotAvailableError

        err = DTNotAvailableError("DEVONthink is not running")
        assert "DEVONthink" in str(err)


# ── _run_osascript ───────────────────────────────────────────────────────────


class TestRunOsascript:
    """Centralised osascript spawn used by every public selector."""

    def test_happy_path_returns_stdout(self):
        from nexus.devonthink import _run_osascript

        with patch("nexus.devonthink.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["osascript", "-e", "..."],
                returncode=0,
                stdout="UUID-1\t/path/one.pdf\n",
                stderr="",
            )
            result = _run_osascript('tell application id "DNtp" to ...', timeout=10)
        assert result == "UUID-1\t/path/one.pdf\n"

    def test_timeout_propagates(self):
        from nexus.devonthink import _run_osascript

        def raising(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=1)

        with patch("nexus.devonthink.subprocess.run", side_effect=raising):
            with pytest.raises(subprocess.TimeoutExpired):
                _run_osascript("anything", timeout=1)

    def test_application_not_running_raises_dt_not_available(self):
        from nexus.devonthink import DTNotAvailableError, _run_osascript

        with patch("nexus.devonthink.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["osascript", "-e", "..."],
                returncode=1,
                stdout="",
                stderr="execution error: Application isn't running. (-600)",
            )
            with pytest.raises(DTNotAvailableError, match="DEVONthink"):
                _run_osascript("anything", timeout=10)


# ── Platform gate across selectors ───────────────────────────────────────────


class TestPlatformGate:
    """Public selectors must refuse to spawn osascript on non-darwin."""

    def test_dt_selection_non_darwin_raises(self, monkeypatch):
        from nexus.devonthink import DTNotAvailableError, _dt_selection

        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(DTNotAvailableError, match="macOS-only"):
            _dt_selection()

    def test_dt_uuid_record_non_darwin_raises_when_no_resolver_injected(
        self, monkeypatch,
    ):
        from nexus.devonthink import DTNotAvailableError, _dt_uuid_record

        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(DTNotAvailableError, match="macOS-only"):
            _dt_uuid_record("ANY-UUID")

    def test_dt_tag_records_non_darwin_raises(self, monkeypatch):
        from nexus.devonthink import DTNotAvailableError, _dt_tag_records

        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(DTNotAvailableError, match="macOS-only"):
            _dt_tag_records("any-tag")

    def test_dt_group_records_non_darwin_raises(self, monkeypatch):
        from nexus.devonthink import DTNotAvailableError, _dt_group_records

        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(DTNotAvailableError, match="macOS-only"):
            _dt_group_records("/Inbox")

    def test_dt_smart_group_records_non_darwin_raises(self, monkeypatch):
        from nexus.devonthink import DTNotAvailableError, _dt_smart_group_records

        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(DTNotAvailableError, match="macOS-only"):
            _dt_smart_group_records("Recent PDFs")


# ── _dt_selection ────────────────────────────────────────────────────────────


class TestDtSelection:
    def test_returns_records_from_canned_output(self, monkeypatch):
        from nexus.devonthink import _dt_selection

        monkeypatch.setattr("sys.platform", "darwin")
        canned = "UUID-A\t/Users/x/A.pdf\nUUID-B\t/Users/x/B.md\n"
        monkeypatch.setattr(
            "nexus.devonthink._run_osascript",
            lambda script, timeout: canned,
        )
        result = _dt_selection()
        assert result == [
            ("UUID-A", "/Users/x/A.pdf"),
            ("UUID-B", "/Users/x/B.md"),
        ]

    def test_empty_selection_returns_empty_list(self, monkeypatch):
        from nexus.devonthink import _dt_selection

        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr(
            "nexus.devonthink._run_osascript",
            lambda script, timeout: "",
        )
        assert _dt_selection() == []

    def test_uses_selected_records_applescript_token(self, monkeypatch):
        """sdef-canonical form is ``selected records`` (per
        nexus_rdr/099-research-5)."""
        from nexus.devonthink import _dt_selection

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return ""

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        _dt_selection()
        assert any("selected records" in s for s in scripts)


# ── _dt_uuid_record ──────────────────────────────────────────────────────────


class TestDtUuidRecord:
    def test_with_injected_resolver(self):
        """Injected resolver bypasses the platform gate (resolver is the
        contract). Production passes no resolver and falls through."""
        from nexus.devonthink import _dt_uuid_record

        def resolver(uuid):
            return "/Users/x/file.pdf", ""

        result = _dt_uuid_record("UUID-X", dt_resolver=resolver)
        assert result == [("UUID-X", "/Users/x/file.pdf")]

    def test_resolver_returning_no_path_yields_empty(self):
        from nexus.devonthink import _dt_uuid_record

        def resolver(uuid):
            return None, "DEVONthink record 'UUID-X' not found"

        result = _dt_uuid_record("UUID-X", dt_resolver=resolver)
        assert result == []

    def test_default_resolver_used_when_none_injected(self, monkeypatch):
        """Without ``dt_resolver`` the impl reuses
        ``aspect_readers._devonthink_resolver_default`` (audit fix F2)
        rather than re-implementing the single-UUID osascript path."""
        from nexus.devonthink import _dt_uuid_record

        monkeypatch.setattr("sys.platform", "darwin")
        seen: list[str] = []

        def fake_default(uuid):
            seen.append(uuid)
            return "/p.pdf", ""

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default",
            fake_default,
        )
        result = _dt_uuid_record("UUID-DEFAULT")
        assert seen == ["UUID-DEFAULT"]
        assert result == [("UUID-DEFAULT", "/p.pdf")]


# ── _dt_tag_records ──────────────────────────────────────────────────────────


class TestDtTagRecords:
    def test_empty_tag_short_circuits(self, monkeypatch):
        """Empty tag means "no filter" — DT would happily return every
        record, which is never what the operator meant. Short-circuit
        without spawning osascript."""
        from nexus.devonthink import _dt_tag_records

        monkeypatch.setattr("sys.platform", "darwin")

        def must_not_spawn(*args, **kwargs):
            pytest.fail("_run_osascript should not be called for empty tag")

        monkeypatch.setattr("nexus.devonthink._run_osascript", must_not_spawn)
        assert _dt_tag_records("") == []

    def test_multi_database_iteration_when_database_none(self, monkeypatch):
        from nexus.devonthink import _dt_tag_records

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return "U1\t/p1.pdf\nU2\t/p2.pdf\n"

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        result = _dt_tag_records("research")
        assert ("U1", "/p1.pdf") in result
        assert ("U2", "/p2.pdf") in result
        joined = "\n".join(scripts)
        # sdef-canonical form must appear; multi-DB iteration must be
        # visible in the script (no specific database name pinned).
        assert "lookup records with tags" in joined
        assert "databases" in joined

    def test_single_database_scoping(self, monkeypatch):
        from nexus.devonthink import _dt_tag_records

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return "U1\t/p1.pdf\n"

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        _dt_tag_records("research", database="MyDB")
        joined = "\n".join(scripts)
        assert "lookup records with tags" in joined
        assert '"MyDB"' in joined

    def test_dedupe_by_uuid_across_databases(self, monkeypatch):
        """Replicated records share a UUID across databases. Helper
        dedupes — caller sees each UUID once."""
        from nexus.devonthink import _dt_tag_records

        monkeypatch.setattr("sys.platform", "darwin")
        canned = "DUP\t/lib1/file.pdf\nDUP\t/lib2/file.pdf\nOTHER\t/x.md\n"
        monkeypatch.setattr(
            "nexus.devonthink._run_osascript",
            lambda *a, **kw: canned,
        )
        result = _dt_tag_records("any")
        uuids = [u for u, _ in result]
        assert uuids.count("DUP") == 1
        assert "OTHER" in uuids


# ── _dt_group_records ────────────────────────────────────────────────────────


class TestDtGroupRecords:
    def test_multi_database_iteration_when_database_none(self, monkeypatch):
        from nexus.devonthink import _dt_group_records

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return "U1\t/p1.pdf\n"

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        _dt_group_records("/Research/Papers")
        assert any("databases" in s for s in scripts)

    def test_trash_root_is_valid(self, monkeypatch):
        from nexus.devonthink import _dt_group_records

        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr(
            "nexus.devonthink._run_osascript",
            lambda *a, **kw: "T1\t/tr/a.pdf\n",
        )
        result = _dt_group_records("/Trash")
        assert result == [("T1", "/tr/a.pdf")]

    def test_tags_root_is_valid(self, monkeypatch):
        from nexus.devonthink import _dt_group_records

        monkeypatch.setattr("sys.platform", "darwin")
        monkeypatch.setattr(
            "nexus.devonthink._run_osascript",
            lambda *a, **kw: "G1\t/tg/a.pdf\n",
        )
        result = _dt_group_records("/Tags")
        assert result == [("G1", "/tg/a.pdf")]

    def test_single_database_scoping(self, monkeypatch):
        from nexus.devonthink import _dt_group_records

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return ""

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        _dt_group_records("/Inbox", database="MyLib")
        assert any('"MyLib"' in s for s in scripts)


# ── _dt_smart_group_records ──────────────────────────────────────────────────


class TestDtSmartGroupRecords:
    def test_three_property_read_uses_canonical_tokens(self, monkeypatch):
        """The smart-group helper must read ``search predicates`` (PLURAL
        — sdef-canonical, per nexus_rdr/099-research-5/-6), ``search
        group`` (the user-authored scope), and ``exclude subgroups``
        (recursion behaviour), then re-execute the search using the
        same scope rather than collapsing to root-of-database."""
        from nexus.devonthink import _dt_smart_group_records

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return "S1\t/s/a.pdf\nS2\t/s/b.md\n"

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        result = _dt_smart_group_records("Recent PDFs")
        assert result == [("S1", "/s/a.pdf"), ("S2", "/s/b.md")]
        joined = "\n".join(scripts)
        assert "parents whose record type is smart group" in joined
        assert "search predicates" in joined  # PLURAL — locked
        assert "search group" in joined
        assert "exclude subgroups" in joined

    def test_missing_value_scope_does_not_crash(self, monkeypatch):
        """When ``search group`` is ``missing value`` (smart group
        scoped at database root), the helper must fall through to
        whole-database search rather than raising."""
        from nexus.devonthink import _dt_smart_group_records

        monkeypatch.setattr("sys.platform", "darwin")

        def fake(script, timeout):
            return "R1\t/r/a.pdf\n"

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        result = _dt_smart_group_records("Library Wide")
        assert result == [("R1", "/r/a.pdf")]

    def test_single_database_scoping(self, monkeypatch):
        from nexus.devonthink import _dt_smart_group_records

        monkeypatch.setattr("sys.platform", "darwin")
        scripts: list[str] = []

        def fake(script, timeout):
            scripts.append(script)
            return ""

        monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
        _dt_smart_group_records("Recent PDFs", database="MyLib")
        assert any('"MyLib"' in s for s in scripts)
