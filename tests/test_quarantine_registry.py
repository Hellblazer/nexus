# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): the flake quarantine registry, its candidates reader, the junit tally.

The registry is ``tests/quarantine.toml``. Every entry carries a 14-day deadline that is CHECKED, never trusted
(Sam, 2026-11-27: a quarantine lasts 14 days, then the test is fixed or deleted). These tests pin the format, the
refusals, the candidates reader that ``scripts/quarantine_report.py add`` uses (the shape colophon's
``red_run_report.py candidates`` writes), and how a pass's junit becomes "passed N of N".
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
JAVA = "ChashVectorConcurrencyTest::concurrentChashAndVectorUpserts_noServerErrors"
TODAY = date(2026, 11, 27)


def _block(nodeid: str = NODE, kind: str = "pytest", since: str = "2026-11-27", deadline: str = "2026-12-11", **over: str) -> str:
    fields = {"bead": "nexus-x1", "reason": "flaky on the pool", "source": SHA, **over}
    return (
        "[[quarantine]]\n"
        f'nodeid = "{nodeid}"\n'
        f'kind = "{kind}"\n'
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
        assert entry == reg.Entry(NODE, "pytest", date(2026, 11, 27), date(2026, 12, 11), "nexus-x1", "flaky on the pool", SHA)

    def test_a_java_entry_parses(self) -> None:
        (entry,) = reg.parse(_block(nodeid=JAVA, kind="java"))
        assert (entry.nodeid, entry.kind) == (JAVA, "java")

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

    @pytest.mark.parametrize("key", ["nodeid", "kind", "since", "deadline", "bead", "reason", "source"])
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

    @pytest.mark.parametrize(
        ("nodeid", "kind"),
        [
            ("tests/test_alpha.py", "pytest"),  # a file, not a test
            ("test_a_bare_function_name", "pytest"),  # no file: cannot be collected
            ("tests/test_alpha.txt::test_x", "pytest"),
            ("tests/test_alpha.py::test_one", "java"),  # a pytest id under java
            (JAVA, "pytest"),  # a java id under pytest
            ("Class#method", "java"),
        ],
    )
    def test_a_nodeid_that_does_not_fit_its_kind_is_refused(self, nodeid: str, kind: str) -> None:
        with pytest.raises(reg.RegistryError, match="nodeid"):
            reg.parse(_block(nodeid=nodeid, kind=kind))

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="kind"):
            reg.parse(_block(kind="rust"))

    def test_an_empty_reason_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="reason"):
            reg.parse(_block(reason=""))

    def test_a_duplicate_nodeid_is_refused(self) -> None:
        with pytest.raises(reg.RegistryError, match="twice"):
            reg.parse(_block() + "\n" + _block())

    def test_broken_toml_is_a_registry_error_not_a_traceback(self) -> None:
        with pytest.raises(reg.RegistryError, match="TOML"):
            reg.parse("[[quarantine\n")


class TestPathSource:
    def test_an_entry_may_carry_a_known_path_source(self) -> None:
        (e,) = reg.parse(_block() + 'path_source = "traceback"\n')
        assert e.path_source == "traceback"

    @pytest.mark.parametrize("value", ["none", "java", ""])
    def test_an_unknown_path_source_is_refused(self, value: str) -> None:
        with pytest.raises(reg.RegistryError, match="path_source"):
            reg.parse(_block() + f'path_source = "{value}"\n')

    def test_render_then_parse_round_trips_with_a_path_source(self) -> None:
        e = reg.Entry(NODE, "pytest", date(2026, 11, 27), date(2026, 12, 11), "b", "r", SHA, "summary")
        assert reg.parse(reg.render(e)) == [e]


class TestAge:
    def _entry(self) -> reg.Entry:
        return reg.parse(_block())[0]

    def test_age_and_days_left_on_the_day_it_starts(self) -> None:
        e = self._entry()
        assert (e.age_days(TODAY), e.days_left(TODAY)) == (0, 14)

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
        e = reg.Entry(NODE, "pytest", date(2026, 11, 27), date(2026, 12, 11), "nexus-x1", 'quote " and \\ slash', SHA)
        assert reg.parse(reg.render(e)) == [e]

    def test_a_parametrised_nodeid_round_trips(self) -> None:
        e = reg.Entry("tests/a.py::T::test[x-1]", "pytest", date(2026, 1, 1), date(2026, 1, 15), "b", "r", SHA)
        assert reg.parse(reg.render(e)) == [e]

    def test_the_surefire_selector_names_only_java_entries(self) -> None:
        entries = reg.parse(_block() + "\n" + _block(nodeid=JAVA, kind="java"))
        assert reg.surefire_selector(entries) == "ChashVectorConcurrencyTest#concurrentChashAndVectorUpserts_noServerErrors"
        assert reg.surefire_selector([]) == ""
        # surefire runs ONLY the excluded names' complement when -Dtest is exclusions alone (measured on 3.5.2), so the
        # exclusion names surefire's default includes first
        assert reg.surefire_exclusion(entries) == (
            "Test*,*Test,*Tests,*TestCase,!ChashVectorConcurrencyTest#concurrentChashAndVectorUpserts_noServerErrors"
        )
        assert reg.surefire_exclusion([]) == ""


UNATTRIBUTED = "tests/test_gamma.py::test_three"
JAVA_U = "SomeIntegrationTest::someMethod"


def _row(nodeid: str, kind: str = "test", path_source: str | None = None) -> dict:
    row = {"nodeid": nodeid, "kind": kind, "red_runs": 3, "first_red": "2026-10-01", "last_red": "2026-11-20",
           "venues": ["self-hosted"], "rule": "flake:timing-message", "run_ids": [1, 2, 3]}
    row["path_source"] = path_source or ("java" if kind == "java" else "summary")  # colophon's: summary|traceback|none, java for java
    return row


def _candidates(tmp_path: Path, candidates: list[tuple[str, str]] | None = None, unattributed: list[tuple[str, str]] | None = None) -> Path:
    doc = {
        "header": {"window": ["2026-09-23", "2026-11-23"], "archive_sha256": "1" * 64, "extraction_sha256": "2" * 64,
                   "script_sha256": "3" * 64, "path_sources": {"summary": 1, "java": 1}, "candidate_tests": 2,
                   "unattributed_tests": 2, "red_runs": 9, "snapshot_final": True, "extraction": "x.jsonl.gz"},
        "candidates": [_row(n, k) for n, k in ([(NODE, "test"), (JAVA, "java")] if candidates is None else candidates)],
        "unattributed": [_row(n, k) for n, k in ([(UNATTRIBUTED, "test"), (JAVA_U, "java")] if unattributed is None else unattributed)],
    }
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(doc))
    return path


def _add(registry: Path, cand: Path, nodeids: list[str], **kw) -> list[reg.Entry]:
    kw.setdefault("bead", "nexus-x1")
    kw.setdefault("reason", "flake")
    kw.setdefault("today", TODAY)
    return reg.add_entries(registry, cand, nodeids, **kw)


class TestCandidates:
    def test_reads_the_header_both_arrays_and_the_file_hash(self, tmp_path: Path) -> None:
        path = _candidates(tmp_path)
        c = reg.read_candidates(path)
        assert c.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert c.header["window"] == ["2026-09-23", "2026-11-23"]
        assert c.nodeids() == {NODE, JAVA}  # `candidates` only
        assert c.unattributed_nodeids() == {UNATTRIBUTED, JAVA_U}

    def test_reads_a_gzipped_file_and_hashes_the_bytes_as_committed(self, tmp_path: Path) -> None:
        import gzip

        plain = _candidates(tmp_path)
        gz = tmp_path / "candidates.json.gz"
        gz.write_bytes(gzip.compress(plain.read_bytes(), mtime=0))
        c = reg.read_candidates(gz)
        assert c.nodeids() == {NODE, JAVA}
        assert c.sha256 == hashlib.sha256(gz.read_bytes()).hexdigest() != hashlib.sha256(plain.read_bytes()).hexdigest()

    def test_a_truncated_gzip_is_a_registry_error(self, tmp_path: Path) -> None:
        import gzip

        gz = tmp_path / "c.json.gz"
        gz.write_bytes(gzip.compress(_candidates(tmp_path).read_bytes())[:-20])
        with pytest.raises(reg.RegistryError, match="gzip"):
            reg.read_candidates(gz)

    def test_a_row_kind_maps_to_a_registry_kind(self, tmp_path: Path) -> None:
        c = reg.read_candidates(_candidates(tmp_path))
        assert (c.kind_of(NODE), c.kind_of(JAVA), c.kind_of(UNATTRIBUTED)) == ("pytest", "java", "pytest")

    @pytest.mark.parametrize(
        "doc",
        [
            "[]",
            "not json",
            '{"candidates": [], "unattributed": 3}',
            '{"candidates": [{"nodeid": "a::b", "kind": "test"}]}',  # no unattributed
            '{"candidates": [{"red_runs": 1, "kind": "test"}], "unattributed": []}',  # no nodeid
            '{"candidates": [{"nodeid": "a::b", "kind": "rust"}], "unattributed": []}',  # unknown kind
            '{"flake_infra": [], "unattributed": []}',  # an earlier shape of the key
            '{"rows": [], "unattributed": []}',
        ],
    )
    def test_a_file_of_the_wrong_shape_is_a_registry_error(self, tmp_path: Path, doc: str) -> None:
        path = tmp_path / "c.json"
        path.write_text(doc)
        with pytest.raises(reg.RegistryError):
            reg.read_candidates(path)


class TestAdd:
    def _registry(self, tmp_path: Path, text: str = "") -> Path:
        path = tmp_path / "quarantine.toml"
        path.write_text(text)
        return path

    def test_adds_a_candidate_with_its_kind_a_checked_deadline_and_the_file_hash(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = self._registry(tmp_path, "# header stays\n")
        added = _add(registry, cand, [NODE, JAVA])
        assert [(e.nodeid, e.kind) for e in added] == [(NODE, "pytest"), (JAVA, "java")]
        text = registry.read_text()
        assert text.startswith("# header stays\n")
        entries = reg.load(registry)
        assert all(e.deadline == e.since + timedelta(days=14) and e.since == TODAY for e in entries)
        assert {e.source for e in entries} == {hashlib.sha256(cand.read_bytes()).hexdigest()}
        assert UNATTRIBUTED not in text

    def test_refuses_a_nodeid_the_candidates_file_does_not_name(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="not named by the candidates file"):
            _add(registry, _candidates(tmp_path), ["tests/test_zzz.py::test_nope"])
        assert registry.read_text() == ""

    def test_refuses_an_unattributed_nodeid_without_a_review_and_says_whose_decision_it_is(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="unattributed.*--review"):
            _add(registry, _candidates(tmp_path), [UNATTRIBUTED])
        assert registry.read_text() == ""

    def test_accepts_one_unattributed_nodeid_with_a_review_and_stores_the_ref_in_the_reason(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        _add(registry, _candidates(tmp_path), [UNATTRIBUTED], review="2026-12-11 review, decision 3")
        (entry,) = reg.load(registry)
        assert entry.nodeid == UNATTRIBUTED
        assert "2026-12-11 review, decision 3" in entry.reason and entry.reason.startswith("flake")

    def test_a_review_covers_one_nodeid_per_call(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="one nodeid per call"):
            _add(registry, _candidates(tmp_path), [UNATTRIBUTED, NODE], review="r")
        assert registry.read_text() == ""

    def test_a_review_does_not_widen_the_refusal_of_an_unnamed_nodeid(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="not named"):
            _add(registry, _candidates(tmp_path), ["tests/x.py::nope"], review="r")

    def test_one_bad_nodeid_adds_none(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError):
            _add(registry, _candidates(tmp_path), [NODE, "tests/x.py::nope"])
        assert registry.read_text() == ""

    def test_refuses_a_nodeid_already_in_the_registry(self, tmp_path: Path) -> None:
        cand = _candidates(tmp_path)
        registry = self._registry(tmp_path)
        _add(registry, cand, [NODE])
        before = registry.read_text()
        with pytest.raises(reg.RegistryError, match="twice"):
            _add(registry, cand, [NODE])
        assert registry.read_text() == before

    def test_refuses_an_empty_nodeid_list(self, tmp_path: Path) -> None:
        with pytest.raises(reg.RegistryError, match="no nodeid"):
            _add(self._registry(tmp_path), _candidates(tmp_path), [])

    def test_a_pytest_candidate_the_report_found_no_path_for_is_refused_and_says_why(self, tmp_path: Path) -> None:
        bare = "tests/test_dry.py::test_a_dry_run"
        cand = tmp_path / "c.json"
        doc = json.loads(_candidates(tmp_path).read_text())
        doc["candidates"] = [_row(bare, "test", path_source="none")]
        cand.write_text(json.dumps(doc))
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match=r"path_source none.*no run can collect it"):
            _add(registry, cand, [bare])
        assert registry.read_text() == ""

    def test_a_pytest_candidate_whose_row_carries_no_file_path_is_refused_and_says_so(self, tmp_path: Path) -> None:
        bare = "test_a_dry_run_never_touches_the_row"
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="candidates row carries no file path"):
            _add(registry, _candidates(tmp_path, candidates=[(bare, "test")]), [bare])
        assert registry.read_text() == ""

    def test_a_pathless_unattributed_row_is_refused_for_the_path_before_the_review_matters(self, tmp_path: Path) -> None:
        bare = "test_bare"
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="no file path"):
            _add(registry, _candidates(tmp_path, unattributed=[(bare, "test")]), [bare], review="r")

    @pytest.mark.parametrize("source", ["summary", "traceback"])
    def test_path_source_of_a_pytest_row_is_stored_in_the_entry(self, tmp_path: Path, source: str) -> None:
        cand = tmp_path / "c.json"
        doc = json.loads(_candidates(tmp_path).read_text())
        doc["candidates"] = [_row(NODE, "test", path_source=source)]
        cand.write_text(json.dumps(doc))
        registry = self._registry(tmp_path)
        _add(registry, cand, [NODE])
        (entry,) = reg.load(registry)
        assert entry.path_source == source and f'path_source = "{source}"' in registry.read_text()

    def test_a_java_row_stores_no_path_source(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        _add(registry, _candidates(tmp_path), [JAVA])
        (entry,) = reg.load(registry)
        assert entry.path_source is None and "path_source" not in registry.read_text()

    def test_a_missing_candidates_or_registry_file_is_a_registry_error(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="candidates file"):
            _add(registry, tmp_path / "absent.json", [NODE])
        with pytest.raises(reg.RegistryError, match="registry"):
            _add(tmp_path / "absent.toml", _candidates(tmp_path), [NODE])

    @pytest.mark.parametrize("since", [date(2026, 11, 28), date(2027, 1, 1)])
    def test_refuses_a_since_in_the_future(self, tmp_path: Path, since: date) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="future"):
            _add(registry, _candidates(tmp_path), [NODE], since=since)
        assert registry.read_text() == ""

    def test_refuses_a_since_more_than_a_day_back(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        with pytest.raises(reg.RegistryError, match="backdated"):
            _add(registry, _candidates(tmp_path), [NODE], since=date(2026, 11, 25))
        assert registry.read_text() == ""

    def test_yesterday_and_today_are_allowed(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        _add(registry, _candidates(tmp_path), [NODE], since=date(2026, 11, 26))
        _add(registry, _candidates(tmp_path), [JAVA], since=TODAY)
        assert [e.since for e in reg.load(registry)] == [date(2026, 11, 26), TODAY]

    def test_the_write_is_atomic_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path)
        _add(registry, _candidates(tmp_path), [NODE])
        assert sorted(p.name for p in tmp_path.iterdir()) == ["candidates.json", "quarantine.toml"]

    def test_a_failed_validation_leaves_no_temp_file(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path, _block())
        with pytest.raises(reg.RegistryError):
            _add(registry, _candidates(tmp_path), [NODE])
        assert sorted(p.name for p in tmp_path.iterdir()) == ["candidates.json", "quarantine.toml"]


def _junit(tmp_path: Path, cases: list[tuple[str, str, str]], name: str = "j.xml") -> Path:
    body = ""
    for classname, test, outcome in cases:
        inner = {"failed": '<failure message="x"/>', "error": '<error message="x"/>', "skipped": '<skipped message="s"/>', "passed": ""}[outcome]
        body += f'<testcase classname="{classname}" name="{test}">{inner}</testcase>'
    path = tmp_path / name
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}</testsuite></testsuites>')
    return path


class TestJunit:
    def test_pytest_key_is_pytests_own_junit_address(self) -> None:
        assert reg.pytest_junit_key("tests/hooks/test_x.py::TestA::test_b[p-1]") == ("tests.hooks.test_x.TestA", "test_b[p-1]")
        assert reg.pytest_junit_key("tests/test_x.py::test_b") == ("tests.test_x", "test_b")

    def test_the_key_matches_what_real_pytest_writes(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile(test_k="""
            import pytest
            class TestK:
                @pytest.mark.parametrize("p", ["a-b", 2])
                def test_m(self, p): pass
            def test_plain(): pass
        """)
        pytester.runpytest_subprocess("--junitxml=out.xml", "-p", "no:cacheprovider")
        cases = reg.read_junit([pytester.path / "out.xml"])
        got = {(c, n) for c, n, _ in cases}
        for node in ("test_k.py::TestK::test_m[a-b]", "test_k.py::TestK::test_m[2]", "test_k.py::test_plain"):
            assert reg.pytest_junit_key(node) in got

    def test_tally_counts_each_outcome_of_the_entrys_testcases_only(self, tmp_path: Path) -> None:
        entry = reg.parse(_block(nodeid="tests/test_alpha.py::TestK::test_m[1]"))[0]
        cases = reg.read_junit([_junit(tmp_path, [
            ("tests.test_alpha.TestK", "test_m[1]", "passed"),
            ("tests.test_alpha.TestK", "test_m[1]", "failed"),
            ("tests.test_alpha.TestK", "test_m[1]", "error"),
            ("tests.test_alpha.TestK", "test_m[1]", "skipped"),
            ("tests.test_alpha.TestK", "test_m[2]", "failed"),  # another parameter
            ("tests.test_beta", "test_m[1]", "failed"),  # another file
        ])])
        t = reg.tally(entry, cases)
        assert (t.passed, t.failed, t.skipped, t.runs, t.examined) == (1, 2, 1, 4, 3)
        assert t.text() == "passed 1 of 4, failed 2 of 4, skipped 1 of 4"

    def test_a_java_entry_matches_fqcn_and_junit5_method_names(self, tmp_path: Path) -> None:
        entry = reg.parse(_block(nodeid=JAVA, kind="java"))[0]
        fq = "dev.nexus.service.ChashVectorConcurrencyTest"
        cases = reg.read_junit([_junit(tmp_path, [
            (fq, "concurrentChashAndVectorUpserts_noServerErrors()", "passed"),
            (fq, "concurrentChashAndVectorUpserts_noServerErrors", "failed"),
            (fq, "otherMethod()", "failed"),
            ("dev.nexus.service.OtherTest", "concurrentChashAndVectorUpserts_noServerErrors()", "failed"),
        ])])
        assert reg.tally(entry, cases) == reg.Tally(passed=1, failed=1, skipped=0)

    def test_text_for_no_result_and_for_only_skips(self) -> None:
        assert reg.Tally().text() == "no result"
        t = reg.Tally(skipped=5)
        assert t.text() == "passed 0 of 5, skipped 5 of 5 (not examined)" and t.examined == 0
        assert reg.Tally(passed=5).text() == "passed 5 of 5"

    def test_a_directory_is_searched_and_a_malformed_file_is_an_error(self, tmp_path: Path) -> None:
        _junit(tmp_path, [("a", "b", "passed")], "x.xml")
        assert len(reg.read_junit([tmp_path])) == 1
        (tmp_path / "bad.xml").write_text("<not closed")
        with pytest.raises(reg.RegistryError, match="junit"):
            reg.read_junit([tmp_path])

    def test_a_missing_junit_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(reg.RegistryError, match="junit"):
            reg.read_junit([tmp_path / "absent.xml"])


class TestClassifyExit:
    """Exit-code policy of one nightly pass: a failing quarantined test is the expected state, a guard verdict is not."""

    FAILED = [("c", "n", "failed")]
    PASSED = [("c", "n", "passed")]

    def test_exit_0_is_fine(self) -> None:
        assert reg.classify_exit(0, self.PASSED)[0]

    def test_exit_1_with_a_failed_testcase_is_fine(self) -> None:
        ok, why = reg.classify_exit(1, self.FAILED)
        assert ok and "quarantined tests failed" in why

    def test_exit_1_with_only_passing_testcases_is_a_guard_verdict_and_red(self) -> None:
        ok, why = reg.classify_exit(1, self.PASSED)
        assert not ok and "guard" in why

    def test_exit_1_with_no_testcases_is_red(self) -> None:
        assert not reg.classify_exit(1, [])[0]

    def test_an_errored_testcase_counts_as_failed(self, tmp_path: Path) -> None:
        cases = reg.read_junit([_junit(tmp_path, [("c", "n", "error")])])
        assert reg.classify_exit(1, cases)[0]

    def test_a_skip_alone_does_not_excuse_exit_1(self) -> None:
        assert not reg.classify_exit(1, [("c", "n", "skipped")])[0]

    @pytest.mark.parametrize("rc", [2, 3, 4, 5, 137])
    def test_every_other_exit_is_red_even_with_failed_testcases(self, rc: int) -> None:
        assert not reg.classify_exit(rc, self.FAILED)[0]

    def test_exit_5_says_nothing_was_selected(self) -> None:
        assert "no tests selected" in reg.classify_exit(5, [])[1]
