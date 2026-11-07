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


def test_an_unreadable_property_fails_open_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """An older DEVONthink has no such property: the record is not excluded, the batch is not lost, and the
    warning names the record (a silent false would hide a DT 4 read failure)."""
    _darwin(monkeypatch, "U1\t/a.md\t?\n")
    with capture_logs() as logs:
        recs = dt_mod._dt_selection()
    assert [r.excluded for r in recs] == [False]
    assert any(e["event"] == "dt_exclusion_unreadable" and e["uuid"] == "U1" for e in logs), logs


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
    _darwin(monkeypatch, "?")
    assert dt_mod._dt_record_excluded("U9") is False


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
