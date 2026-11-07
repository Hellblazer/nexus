# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 Step 3.4 (nexus-rfvyy.8.5, Q7 / assumption A6): ``nx dt index`` registers a record
the operator excluded from AI in DEVONthink as ``sensitive``.

A6 (spike nexus-rfvyy.8.1, ``nexus_rdr/243-research-12``): DEVONthink 4 exposes the exclusion as one
boolean AppleScript property, ``exclude from chat``, on the record and on its database; the database flag
takes priority. The read is enumeration only: this RDR adds no write to DEVONthink.

Nothing here needs the app: the osascript seam (``nexus.devonthink._run_osascript``) and the per-record
indexer (``nexus.commands.dt._index_record``) are faked, and the engine capability probe is faked at
``nexus.sensitive_write.require_sensitive_engine``. The mode-lint exemption: the Voyage token in this file is
a collection-name label at module level; each test names its mode.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

import nexus.commands.dt as dtcmd
import nexus.devonthink as dt_mod
from nexus.cli import main
from nexus.devonthink import DtRecord
from tests._module_seam import setattr_in

BGE = "bge-base-en-v15-768"
_VOYAGE_DOCS = "docs__rfvyy85__voyage-context-3__v1"

_PROP = "exclude from chat"


def _darwin(monkeypatch: pytest.MonkeyPatch, canned: str, scripts: list[str] | None = None) -> None:
    setattr_in(monkeypatch, "nexus.devonthink", "sys.platform", "darwin")

    def fake(script: str, timeout: int) -> str:
        if scripts is not None:
            scripts.append(script)
        return canned

    monkeypatch.setattr("nexus.devonthink._run_osascript", fake)


# --------------------------------------------------------------------------- #
# Enumeration: the selectors read both properties and carry the answer.
# --------------------------------------------------------------------------- #

def test_the_third_field_marks_a_record_excluded_and_a_legacy_line_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _darwin(monkeypatch, "U1\t/a.md\t1\nU2\t/b.md\t0\nU3\t/c.md\n")
    recs = dt_mod._dt_selection()
    assert recs == [("U1", "/a.md"), ("U2", "/b.md"), ("U3", "/c.md")]  # still plain (uuid, path) pairs
    assert [r.excluded for r in recs] == [True, False, False]


@pytest.fixture(autouse=True)
def _fresh_version_cache():
    """The DEVONthink major version is cached per process; no test inherits another's."""
    dt_mod._reset_dt_major_version()
    yield
    dt_mod._reset_dt_major_version()


def _major(monkeypatch: pytest.MonkeyPatch, major: int) -> None:
    monkeypatch.setattr(dt_mod, "_dt_major_version", lambda: major)


def test_an_unreadable_property_on_dt4_fails_closed_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEVONthink 4 has the property, so "?" is a read failure (a transient AppleScript error, an
    unreadable ancestor group), not 'nothing to honour': the record is treated as excluded, the batch is
    not lost, and the warning names the record."""
    _darwin(monkeypatch, "U1\t/a.md\t?\nU2\t/b.md\t0\n")
    _major(monkeypatch, 4)
    with capture_logs() as logs:
        recs = dt_mod._dt_selection()
    assert [r.excluded for r in recs] == [True, False]
    assert any(e["event"] == "dt_exclusion_unreadable_fail_closed" and e["uuid"] == "U1" for e in logs), logs


def test_an_unreadable_property_before_dt4_reads_not_excluded_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DEVONthink before 4 has no such property: nothing to honour, the record is not excluded, and the
    log line names it."""
    _darwin(monkeypatch, "U1\t/a.md\t?\n")
    _major(monkeypatch, 3)
    with capture_logs() as logs:
        recs = dt_mod._dt_selection()
    assert [r.excluded for r in recs] == [False]
    assert any(e["event"] == "dt_exclusion_unreadable" and e["uuid"] == "U1" for e in logs), logs


def test_an_unreadable_property_with_an_unknown_version_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _darwin(monkeypatch, "U1\t/a.md\t?\n")
    _major(monkeypatch, 0)  # the version read failed or did not parse
    assert [r.excluded for r in dt_mod._dt_selection()] == [True]


def test_the_version_is_read_once_and_only_when_a_property_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, "nexus.devonthink", "sys.platform", "darwin")
    scripts: list[str] = []

    def fake(script: str, timeout: int) -> str:
        scripts.append(script)
        return "4.4.2\n" if script.endswith("return version") else "U1\t/a.md\t?\nU2\t/b.md\t?\n"

    monkeypatch.setattr("nexus.devonthink._run_osascript", fake)
    dt_mod._reset_dt_major_version()
    try:
        assert [r.excluded for r in dt_mod._dt_selection()] == [True, True]
        assert [r.excluded for r in dt_mod._dt_selection()] == [True, True]
        assert sum(1 for sc in scripts if sc.endswith("return version")) == 1, "read once per process"

        scripts.clear()
        dt_mod._reset_dt_major_version()
        monkeypatch.setattr("nexus.devonthink._run_osascript", lambda sc, timeout: scripts.append(sc) or "U1\t/a.md\t0\n")
        dt_mod._dt_selection()
        assert len(scripts) == 1, "every property readable: no version read"
    finally:
        dt_mod._reset_dt_major_version()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param("4.4.2\n", 4, id="dt4"),
        pytest.param("3.9.5", 3, id="dt3"),
        pytest.param("", 0, id="empty-is-unknown"),
        pytest.param("beta", 0, id="unparseable-is-unknown"),
    ],
)
def test_the_major_version_parse(monkeypatch: pytest.MonkeyPatch, reply: str, expected: int) -> None:
    monkeypatch.setattr("nexus.devonthink._run_osascript", lambda sc, timeout: reply)
    dt_mod._reset_dt_major_version()
    try:
        assert dt_mod._dt_major_version() == expected
    finally:
        dt_mod._reset_dt_major_version()


def test_a_version_read_that_raises_is_unknown_and_unknown_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(sc: str, timeout: int) -> str:
        raise dt_mod.DTNotAvailableError("not running")

    monkeypatch.setattr("nexus.devonthink._run_osascript", boom)
    dt_mod._reset_dt_major_version()
    try:
        assert dt_mod._dt_major_version() == 0
        assert dt_mod._unreadable_means_excluded("U1") is True
    finally:
        dt_mod._reset_dt_major_version()


def test_a_path_with_a_tab_keeps_its_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    _darwin(monkeypatch, "U1\t/a\tb.md\t1\nU2\t/c\td.md\n")
    recs = dt_mod._dt_selection()
    assert [(r[0], r[1], r.excluded) for r in recs] == [("U1", "/a\tb.md", True), ("U2", "/c\td.md", False)]


def test_a_duplicate_uuid_keeps_the_first_and_its_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _darwin(monkeypatch, "U1\t/a.md\t1\nU1\t/a.md\t0\n")
    recs = dt_mod._dt_tag_records("t")
    assert len(recs) == 1 and recs[0].excluded is True


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: dt_mod._dt_selection(), id="selection"),
        pytest.param(lambda: dt_mod._dt_tag_records("t"), id="tag"),
        pytest.param(lambda: dt_mod._dt_group_records("/G"), id="group"),
        pytest.param(lambda: dt_mod._dt_smart_group_records("S"), id="smart-group"),
    ],
)
def test_every_selector_script_reads_the_record_and_its_database(monkeypatch: pytest.MonkeyPatch, call) -> None:
    """The script text names both reads, so neither can be dropped silently (the database flag takes
    priority over the record flag in DEVONthink's own contract)."""
    scripts: list[str] = []
    _darwin(monkeypatch, "U1\t/a.md\t1\n", scripts)
    call()
    assert len(scripts) == 1
    script = scripts[0]
    assert script.count(_PROP) >= 2, script
    assert "database of" in script, script
    # each read is guarded: an unreadable property must not fail the whole batch
    assert script.count("try") >= 2 and "on error" in script, script


def test_the_uuid_selector_probes_the_exclusion_after_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[str] = []
    _darwin(monkeypatch, "1", scripts)
    recs = dt_mod._dt_uuid_record("U9", dt_resolver=lambda u: ("/z.md", ""))
    assert recs == [("U9", "/z.md")] and recs[0].excluded is False  # an injected resolver spawns nothing
    assert scripts == []

    recs = dt_mod._dt_uuid_record("U9", dt_resolver=lambda u: ("/z.md", ""), dt_exclusion=lambda u: True)
    assert recs[0].excluded is True

    monkeypatch.setattr("nexus.aspect_readers._devonthink_resolver_default", lambda u: ("/z.md", ""))
    recs = dt_mod._dt_uuid_record("U9")
    assert recs[0].excluded is True
    (script,) = scripts
    assert script.count(_PROP) >= 2 and "database of" in script and '"U9"' in script, script


def test_the_uuid_probe_reads_false_for_a_record_that_is_not_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    _darwin(monkeypatch, "0")
    assert dt_mod._dt_record_excluded("U9") is False
    _darwin(monkeypatch, "1")
    assert dt_mod._dt_record_excluded("U9") is True


@pytest.mark.parametrize("reply", ["?", "", "\n", "garbage"], ids=["unreadable", "empty", "blank", "other"])
def test_the_uuid_probe_never_returns_false_silently_on_an_unanswered_read(
    monkeypatch: pytest.MonkeyPatch, reply: str,
) -> None:
    """An unreadable property or an empty osascript reply is not an answer: DEVONthink 4 fails closed
    (excluded), before 4 reads not excluded, and the log line is written either way."""
    _darwin(monkeypatch, reply)
    _major(monkeypatch, 4)
    with capture_logs() as logs:
        assert dt_mod._dt_record_excluded("U9") is True
    assert any(e["event"] == "dt_exclusion_unreadable_fail_closed" and e["uuid"] == "U9" for e in logs), logs

    _major(monkeypatch, 3)
    with capture_logs() as logs:
        assert dt_mod._dt_record_excluded("U9") is False
    assert any(e["event"] == "dt_exclusion_unreadable" and e["uuid"] == "U9" for e in logs), logs


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: dt_mod._dt_selection(), id="selection"),
        pytest.param(lambda: dt_mod._dt_tag_records("t"), id="tag"),
        pytest.param(lambda: dt_mod._dt_group_records("/G"), id="group"),
        pytest.param(lambda: dt_mod._dt_smart_group_records("S"), id="smart-group"),
        pytest.param(lambda: dt_mod._dt_record_excluded("U9"), id="uuid-probe"),
    ],
)
def test_every_script_walks_the_ancestor_groups(monkeypatch: pytest.MonkeyPatch, call) -> None:
    """A record inside an excluded group inherits the exclusion: the script reads the flag of every
    ancestor (``parents``, walked up), and a failed ancestor read is "?" (so DT 4 fails closed), never "0"."""
    scripts: list[str] = []
    _darwin(monkeypatch, "U1\t/a.md\t1\n", scripts)
    call()
    script = scripts[0]
    assert "parents of" in script and "repeat while" in script, script
    assert f"({_PROP} of theGroup) is true" in script, script
    assert "root of (database of theGroup)" in script, "the walk stops at the database root instead of reading its parents"
    walk = script[script.index("parents of"):]
    assert 'then set theExcl to "?"' in walk, "a failed ancestor read must set the unreadable mark"


# --------------------------------------------------------------------------- #
# The command: an excluded record registers sensitive, by the explicit --sensitive path.
# --------------------------------------------------------------------------- #

@pytest.fixture
def indexed(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Fake the per-record indexer and the engine probe; collect what each record was indexed with."""
    calls: list[dict] = []

    def fake(uuid, path, **kw):
        calls.append({"uuid": uuid, "path": path, **kw})
        return True, 1

    monkeypatch.setattr(dtcmd, "_index_record", fake)
    monkeypatch.setattr("nexus.sensitive_write.require_sensitive_engine", lambda what: None)
    return calls


def _records(monkeypatch: pytest.MonkeyPatch, *recs) -> None:
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: list(recs))


def _run(*extra: str):
    return CliRunner().invoke(main, ["dt", "index", "--uuid", "X", *extra])


def test_an_excluded_record_is_registered_sensitive_and_the_others_are_not(
    monkeypatch: pytest.MonkeyPatch, indexed: list[dict],
) -> None:
    _records(
        monkeypatch,
        DtRecord("U-EX", "/ex.md", excluded=True),
        DtRecord("U-OK", "/ok.md", excluded=False),
        ("U-PLAIN", "/plain.md"),  # a selector double that still returns a bare pair
    )
    out = _run("--collection", f"docs__rfvyy85__{BGE}__v1")
    assert out.exit_code == 0, out.output
    by = {c["uuid"]: c for c in indexed}
    assert by["U-EX"].get("sensitive") is True
    assert "sensitive" not in by["U-OK"] and "sensitive" not in by["U-PLAIN"]


def test_an_excluded_record_takes_the_route_an_explicit_sensitive_takes(
    monkeypatch: pytest.MonkeyPatch, indexed: list[dict],
) -> None:
    """Local mode, a Voyage collection, no ack: the same record goes to the bge sibling whether the flag came
    from the command line or from DEVONthink; with the ack it keeps its collection."""
    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=True))
    out = _run("--collection", _VOYAGE_DOCS)
    assert out.exit_code == 0, out.output
    assert indexed[-1]["collection"] == f"docs__rfvyy85__{BGE}__v1"

    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=False))
    out = _run("--collection", _VOYAGE_DOCS, "--sensitive")
    assert indexed[-1]["collection"] == f"docs__rfvyy85__{BGE}__v1"

    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=True))
    out = _run("--collection", _VOYAGE_DOCS, "--ack-external-embedder", "--sensitive")
    assert indexed[-1]["collection"] == _VOYAGE_DOCS


def test_cloud_mode_refuses_an_excluded_record_without_the_ack_and_still_indexes_the_rest(
    monkeypatch: pytest.MonkeyPatch, cloud_mode, indexed: list[dict],
) -> None:
    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=True), DtRecord("U-OK", "/ok.md"))
    out = _run()
    assert out.exit_code != 0, out.output
    assert [c["uuid"] for c in indexed] == ["U-OK"], indexed
    assert "cloud mode" in out.output and "--ack-external-embedder" in out.output, out.output
    assert "U-EX" in out.output


def test_cloud_mode_with_the_ack_indexes_an_excluded_record_sensitive(
    monkeypatch: pytest.MonkeyPatch, cloud_mode, indexed: list[dict],
) -> None:
    monkeypatch.setattr("nexus.sensitive_write._cloud_endpoint", lambda: "https://cloud.invalid")  # the disclosure line names it
    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=True))
    out = _run("--ack-external-embedder")
    assert out.exit_code == 0, out.output  # the ack means something here: the record itself is sensitive
    assert "https://cloud.invalid" in out.output, "the cloud disclosure (census row 22) rides the ack"
    assert [(c["uuid"], c.get("sensitive"), c.get("ack_external_embedder")) for c in indexed] == [("U-EX", True, True)]


def test_an_engine_that_cannot_hold_the_flag_refuses_the_excluded_record_before_any_write(
    monkeypatch: pytest.MonkeyPatch, indexed: list[dict],
) -> None:
    def refuse(what: str) -> None:
        raise RuntimeError(f"{what} refused: engine without catalog_sensitivity_supported. Nothing was written.")

    monkeypatch.setattr("nexus.sensitive_write.require_sensitive_engine", refuse)
    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=True), DtRecord("U-OK", "/ok.md"))
    out = _run("--collection", f"docs__rfvyy85__{BGE}__v1")
    assert out.exit_code != 0, out.output
    assert [c["uuid"] for c in indexed] == ["U-OK"], indexed
    assert "Nothing was written" in out.output


def test_the_engine_probe_is_not_run_when_nothing_is_excluded(
    monkeypatch: pytest.MonkeyPatch, indexed: list[dict],
) -> None:
    probes: list[str] = []
    monkeypatch.setattr("nexus.sensitive_write.require_sensitive_engine", lambda what: probes.append(what))
    _records(monkeypatch, DtRecord("U-OK", "/ok.md"))
    assert _run("--collection", f"docs__rfvyy85__{BGE}__v1").exit_code == 0
    assert probes == []


def test_a_dry_run_marks_the_excluded_record_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, indexed: list[dict],
) -> None:
    _records(monkeypatch, DtRecord("U-EX", "/ex.md", excluded=True), DtRecord("U-OK", "/ok.md"))
    out = _run("--dry-run")
    assert out.exit_code == 0 and indexed == [], out.output
    ex_line = next(line for line in out.output.splitlines() if "U-EX" in line)
    ok_line = next(line for line in out.output.splitlines() if "U-OK" in line)
    assert "sensitive" in ex_line and "sensitive" not in ok_line, out.output
