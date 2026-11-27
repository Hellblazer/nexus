# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): the flake quarantine registry, its candidates reader and the scope rule.

The registry is ``tests/quarantine.toml``. Every entry carries a 14-day deadline that is CHECKED, never trusted
(Sam, 2026-11-27: a quarantine lasts 14 days, then the test is fixed or deleted). These tests pin the format, the
refusals, the candidates reader that ``scripts/quarantine_report.py add`` uses, and which runs may check that a
registry entry still names a collected test.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests import _quarantine_registry as reg

SHA = "a" * 64
NODE = "tests/test_alpha.py::test_one"


def _block(nodeid: str = NODE, since: str = "2026-11-27", deadline: str = "2026-12-11", **over: str) -> str:
    fields = {"bead": "nexus-x1", "reason": "flaky on the pool", "source": SHA, **over}
    return (
        "[[quarantine]]\n"
        f'nodeid = "{nodeid}"\n'
        f"since = {since}\n"
        f"deadline = {deadline}\n"
        + "".join(f'{k} = "{v}"\n' for k, v in fields.items())
    )


class TestParse:
    def test_empty_text_is_an_empty_registry(self) -> None:
        assert reg.parse("") == []

    def test_comments_only_is_an_empty_registry(self) -> None:
        assert reg.parse("# nothing quarantined\n") == []

    def test_a_valid_entry_parses_to_dates_and_strings(self) -> None:
        (entry,) = reg.parse(_block())
        assert entry == reg.Entry(
            nodeid=NODE,
            since=date(2026, 11, 27),
            deadline=date(2026, 12, 11),
            bead="nexus-x1",
            reason="flaky on the pool",
            source=SHA,
        )

    def test_the_deadline_is_since_plus_fourteen(self) -> None:
        assert reg.QUARANTINE_DAYS == 14
        assert reg.deadline_for(date(2026, 11, 27)) == date(2026, 12, 11)

    @pytest.mark.parametrize("deadline", ["2026-12-10", "2026-12-12", "2027-01-01"])
    def test_a_deadline_that_is_not_since_plus_fourteen_is_refused(self, deadline: str) -> None:
        with pytest.raises(reg.RegistryError, match="deadline"):
            reg.parse(_block(deadline=deadline))

    def test_a_dateless_string_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="since"):
            reg.parse(_block(since='"2026-11-27"'))

    @pytest.mark.parametrize("key", ["nodeid", "since", "deadline", "bead", "reason", "source"])
    def test_a_missing_key_is_refused_by_name(self, key: str) -> None:
        text = "".join(ln for ln in _block().splitlines(keepends=True) if not ln.startswith(f"{key} = "))
        with pytest.raises(reg.RegistryError, match=key):
            reg.parse(text)

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="owner"):
            reg.parse(_block() + 'owner = "sam"\n')

    def test_a_top_level_key_other_than_the_entry_list_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="quarantined"):
            reg.parse("quarantined = []\n")

    def test_a_source_that_is_not_a_sha256_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="source"):
            reg.parse(_block(source="deadbeef"))

    def test_a_nodeid_without_a_test_part_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="nodeid"):
            reg.parse(_block(nodeid="tests/test_alpha.py"))

    def test_an_empty_reason_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="reason"):
            reg.parse(_block(reason=""))

    def test_a_duplicate_nodeid_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="twice"):
            reg.parse(_block() + "\n" + _block())

    def test_broken_toml_is_a_registry_error_not_a_traceback(self) -> None:
        with pytest.raises(reg.RegistryError, match="TOML"):
            reg.parse("[[quarantine\n")


class TestAge:
    def _entry(self) -> reg.Entry:
        return reg.parse(_block())[0]

    def test_age_and_days_left_on_the_day_it_starts(self) -> None:
        e = self._entry()
        assert (e.age_days(date(2026, 11, 27)), e.days_left(date(2026, 11, 27))) == (0, 14)

    def test_the_deadline_day_is_due_not_overdue(self) -> None:
        e = self._entry()
        assert e.days_left(date(2026, 12, 11)) == 0
        assert not e.overdue(date(2026, 12, 11))

    def test_the_day_after_the_deadline_is_overdue(self) -> None:
        e = self._entry()
        assert e.days_left(date(2026, 12, 12)) == -1
        assert e.overdue(date(2026, 12, 12))


class TestRender:
    def test_render_then_parse_round_trips(self) -> None:
        e = reg.Entry(NODE, date(2026, 11, 27), date(2026, 12, 11), "nexus-x1", 'quote " and \\ slash', SHA)
        assert reg.parse(reg.render(e)) == [e]

    def test_a_parametrised_nodeid_round_trips(self) -> None:
        e = reg.Entry("tests/a.py::T::test[x-1]", date(2026, 1, 1), date(2026, 1, 15), "b", "r", SHA)
        assert reg.parse(reg.render(e)) == [e]


UNATTRIBUTED = "tests/test_gamma.py::test_three"


def _row(nodeid: str) -> dict:
    return {"nodeid": nodeid, "red_runs": 3, "first_red": "2026-10-01", "last_red": "2026-11-20",
            "venues": ["lab-6c18g"], "rule": "flake"}


def _candidates(tmp_path: Path, flake_infra: list[str] | None = None, unattributed: list[str] | None = None) -> Path:
    doc = {
        "window": {"start": "2026-09-27", "end": "2026-11-26"},
        "sha256": "b" * 64,
        "flake_infra": [_row(n) for n in ([NODE] if flake_infra is None else flake_infra)],
        "unattributed": [_row(n) for n in ([UNATTRIBUTED] if unattributed is None else unattributed)],
    }
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(doc))
    return path


class TestCandidates:
    def test_reads_the_window_both_arrays_and_the_file_hash(self, tmp_path: Path) -> None:
        path = _candidates(tmp_path)
        c = reg.read_candidates(path)
        assert c.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert c.window == {"start": "2026-09-27", "end": "2026-11-26"}
        assert c.nodeids() == {NODE}  # flake_infra only
        assert c.unattributed_nodeids() == {UNATTRIBUTED}

    @pytest.mark.parametrize(
        "doc",
        [
            "[]",
            "not json",
            '{"flake_infra": [], "unattributed": 3}',
            '{"flake_infra": [{"red_runs": 1}], "unattributed": []}',
            '{"unattributed": []}',  # the old single-array shape, or a missing class
            '{"rows": [], "unattributed": []}',
        ],
    )
    def test_a_file_of_the_wrong_shape_is_a_registry_error(self, tmp_path: Path, doc: str) -> None:
        path = tmp_path / "c.json"
        path.write_text(doc)
        with pytest.raises(reg.RegistryError):
            reg.read_candidates(path)


class TestAdd:
    def test_adds_the_named_nodeids_with_a_checked_deadline_and_the_file_hash(self, tmp_path: Path) -> None:
        other = "tests/test_beta.py::test_two"
        cand = _candidates(tmp_path, flake_infra=[NODE, other])
        registry = tmp_path / "quarantine.toml"
        registry.write_text("# header stays\n")
        added = reg.add_entries(registry, cand, [NODE], bead="nexus-x1", reason="flake", since=date(2026, 11, 27))
        assert [e.nodeid for e in added] == [NODE]
        text = registry.read_text()
        assert text.startswith("# header stays\n")
        (entry,) = reg.load(registry)
        assert entry.deadline == entry.since + timedelta(days=14)
        assert entry.source == hashlib.sha256(cand.read_bytes()).hexdigest()
        assert other not in text

    def test_refuses_a_nodeid_the_candidates_file_does_not_name(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = tmp_path / "quarantine.toml"
        registry.write_text("")
        with pytest.raises(reg.RegistryError, match="not named by the candidates file"):
            reg.add_entries(registry, cand, ["tests/test_zzz.py::test_nope"], bead="b", reason="r", since=date(2026, 11, 27))
        assert registry.read_text() == ""

    def test_refuses_a_nodeid_only_in_the_unattributed_class_and_says_it_is_sams(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = tmp_path / "quarantine.toml"
        registry.write_text("")
        with pytest.raises(reg.RegistryError, match="unattributed.*Sam"):
            reg.add_entries(registry, cand, [UNATTRIBUTED], bead="b", reason="r", since=date(2026, 11, 27))
        assert registry.read_text() == ""

    def test_a_nodeid_in_both_arrays_is_a_flake_infra_row(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path, unattributed=[NODE])
        registry = tmp_path / "quarantine.toml"
        registry.write_text("")
        reg.add_entries(registry, cand, [NODE], bead="b", reason="r", since=date(2026, 11, 27))
        assert [e.nodeid for e in reg.load(registry)] == [NODE]

    def test_one_bad_nodeid_adds_none(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = tmp_path / "quarantine.toml"
        registry.write_text("")
        with pytest.raises(reg.RegistryError):
            reg.add_entries(registry, cand, [NODE, "tests/x.py::nope"], bead="b", reason="r", since=date(2026, 11, 27))
        assert registry.read_text() == ""

    def test_refuses_a_nodeid_already_in_the_registry(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = tmp_path / "quarantine.toml"
        registry.write_text("")
        reg.add_entries(registry, cand, [NODE], bead="b", reason="r", since=date(2026, 11, 27))
        with pytest.raises(reg.RegistryError, match="twice|already"):
            reg.add_entries(registry, cand, [NODE], bead="b", reason="r", since=date(2026, 11, 28))

    def test_refuses_an_empty_nodeid_list(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = tmp_path / "quarantine.toml"
        registry.write_text("")
        with pytest.raises(reg.RegistryError, match="no nodeid"):
            reg.add_entries(registry, cand, [], bead="b", reason="r", since=date(2026, 11, 27))


class TestArgsCover:
    """A run may check that a registry entry still names a collected test only when the run could have collected it."""

    @pytest.mark.parametrize(
        ("arg", "covers"),
        [
            ("tests", True),
            (".", True),
            ("", True),
            ("tests/test_alpha.py", True),
            ("tests/test_beta.py", False),
            ("tests/test_alpha.py::test_one", True),
            ("tests/test_alpha.py::test_other", False),
            ("tests/test_alp", False),
            ("tests/sub", False),
        ],
    )
    def test_cover_rule(self, arg: str, covers: bool) -> None:
        assert reg.arg_covers(NODE, arg) is covers

    def test_a_directory_arg_covers_only_files_below_it(self) -> None:
        assert reg.arg_covers("tests/sub/test_x.py::test_y", "tests/sub")
        assert not reg.arg_covers("tests/subway/test_x.py::test_y", "tests/sub")

    def test_a_class_or_param_prefix_covers_its_members(self) -> None:
        assert reg.arg_covers("tests/a.py::TestK::test_m", "tests/a.py::TestK")
        assert reg.arg_covers("tests/a.py::test_m[p1]", "tests/a.py::test_m")

    def test_run_covers_when_any_arg_does(self) -> None:
        assert reg.run_covers(NODE, ["tests/test_beta.py", "tests/test_alpha.py"])
        assert not reg.run_covers(NODE, ["tests/test_beta.py"])
