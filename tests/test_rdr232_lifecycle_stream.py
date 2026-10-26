# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.7, assumption A7): the bounded T2 lifecycle stream.

Tenant-free: the stream reads through two narrow seams, the catalog's ``get_owner_by_prefix`` and the
T2 client's exact ``get(project, title)``, so a fake of each stands in and the tests count every call.
The claims against the real catalog and a real second tenant live in
``test_rdr232_golden_fixture.py`` (the one journey that already holds a tenant; the tenant ceiling
forbids another). Each test names the claim it pins.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from nexus.plans.evidence import SourceRef
from nexus.plans.lifecycle_stream import (
    DEFAULT_LIFECYCLE_CAPS,
    LifecycleStream,
    read_lifecycle_stream,
)
from nexus.plans.primary_evidence import NamedSourceResolution, PrimaryEvidence, ResolvedSource, SelectedChunk
from nexus.plans.reserved_stream import RESERVED_SHARE_MAX, ReservedStream, reserved_scope, set_reserved_step
from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _hydrate_operator_args

ART_TUMBLER = "1.2.4686"
NEXUS_TUMBLER = "1.1.77"
ART_META = "id: RDR-084\nprefix: ART\nstatus: accepted\n"
ART_GATE = "outcome: PASSED\ndate: 2026-03-09\nsummary: ART gate record\n"
NEXUS_META = "id: RDR-084\nprefix: NEX\nstatus: draft\n"
NEXUS_GATE = "outcome: BLOCKED\nsummary: NEXUS gate record\n"


class FakeCatalog:
    """Only the owner probe the stream makes; counts every call."""

    def __init__(self, owners: dict[str, dict | None]) -> None:
        self.owners = owners
        self.calls: list[str] = []

    def get_owner_by_prefix(self, prefix: str) -> dict | None:
        self.calls.append(prefix)
        return self.owners.get(prefix)


class FakeClock:
    """A monotonic clock the test moves by hand (``FakeT2`` advances it by ``cost`` per read)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeT2:
    """Exact ``get`` over a ``{(project, title): content}`` map. The only method; no search or listing
    exists on it, so an unbounded read is not expressible through the seam. With a *clock*, each read
    costs *cost* seconds on it, and the ``timeout`` the stream passed is recorded."""

    def __init__(self, rows: dict[tuple[str, str], str], fail_on: set[str] | None = None,
                 clock: FakeClock | None = None, cost: float = 0.0) -> None:
        self.rows = rows
        self.gets: list[tuple[str, str]] = []
        self.timeouts: list[float | None] = []
        self.fail_on = fail_on or set()
        self.clock, self.cost = clock, cost

    def get(self, project: str, title: str, timeout: float | None = None) -> dict | None:
        self.gets.append((project, title))
        self.timeouts.append(timeout)
        if self.clock is not None:
            self.clock.now += self.cost
        if project in self.fail_on:
            raise ConnectionError("t2 down")
        content = self.rows.get((project, title))
        return None if content is None else {"project": project, "title": title, "content": content}


def _owners() -> dict[str, dict | None]:
    return {
        "1.2": {"tumbler_prefix": "1.2", "name": "ART", "owner_type": "repo"},
        "1.1": {"tumbler_prefix": "1.1", "name": "nexus", "owner_type": "repo"},
        "1.9": {"tumbler_prefix": "1.9", "name": "knowledge", "owner_type": "curator"},
        "1.8": {"tumbler_prefix": "1.8", "name": "", "owner_type": "repo"},
    }


def _rows() -> dict[tuple[str, str], str]:
    return {
        ("ART_rdr", "084"): ART_META, ("ART_rdr", "084-gate-latest"): ART_GATE,
        ("nexus_rdr", "084"): NEXUS_META, ("nexus_rdr", "084-gate-latest"): NEXUS_GATE,
    }


def _src(value: str = "RDR-084", *, tumbler: str = ART_TUMBLER, rdr_number: int | None = 84,
         status: str = "resolved", detail: str = "") -> ResolvedSource:
    return ResolvedSource(
        ref=SourceRef.parse(value), status=status,  # type: ignore[arg-type]
        tumbler=tumbler if status == "resolved" else None, title=value, qualified=True,
        rdr_number=rdr_number, detail=detail,
    )


def _resolution(*sources: ResolvedSource) -> NamedSourceResolution:
    return NamedSourceResolution(sources=tuple(sources))


def _read(resolution, *, t2=None, cat=None, caps=DEFAULT_LIFECYCLE_CAPS, **kw) -> tuple[LifecycleStream, FakeT2, FakeCatalog]:
    t2 = t2 or FakeT2(_rows())
    cat = cat or FakeCatalog(_owners())
    stream = read_lifecycle_stream(resolution, cat=cat, t2_get=t2.get, caps=caps, **kw)
    assert stream is not None
    return stream, t2, cat


# ── 1. an RDR source yields its metadata and latest gate record ──────────────────────────────


def test_an_rdr_source_yields_its_metadata_and_latest_gate_record() -> None:
    stream, t2, cat = _read(_resolution(_src()))
    assert [(r.kind, r.project, r.title) for r in stream.records] == [
        ("metadata", "ART_rdr", "084"), ("gate-latest", "ART_rdr", "084-gate-latest")]
    assert [r.text for r in stream.records] == [ART_META.strip(), ART_GATE.strip()]
    (outcome,) = stream.sources
    assert outcome.status == "read" and outcome.project == "ART_rdr" and outcome.rdr_number == 84
    assert t2.gets == [("ART_rdr", "84"), ("ART_rdr", "084"), ("ART_rdr", "084-gate-latest")], (
        "exact gets only: the bare title misses, the padded one hits, the gate record is asked under the shape that hit")
    assert cat.calls == ["1.2"], "the project comes from the resolved document's owner row"


def test_the_records_become_reserved_items_with_a_record_header() -> None:
    stream, _, _ = _read(_resolution(_src()))
    items = stream.reserved_items()
    assert [i.raw for i in items] == [ART_META.strip(), ART_GATE.strip()]
    assert all(i.chash == "" for i in items), "a record has no chash; only the text dedupe applies"
    assert items[0].text.startswith("[primary record ") and "ART_rdr/084" in items[0].text.split("\n", 1)[0]
    assert len({i.token for i in items}) == 2


@pytest.mark.parametrize("shape", ["84", "084", "RDR-84", "RDR-084"])
def test_every_title_shape_the_rdr_tooling_writes_is_found(shape: str) -> None:
    rows = {("ART_rdr", shape): ART_META, ("ART_rdr", f"{shape}-gate-latest"): ART_GATE}
    stream, t2, _ = _read(_resolution(_src()), t2=FakeT2(rows))
    assert [r.title for r in stream.records] == [shape, f"{shape}-gate-latest"]
    assert len(t2.gets) <= 8


def test_the_title_shapes_match_the_rdr_tooling_for_any_number() -> None:
    from nexus.commands.rdr import _t2_rdr_titles
    from nexus.plans.lifecycle_stream import rdr_title_shapes

    for n in (1, 9, 84, 232, 1000):
        assert rdr_title_shapes(n) == _t2_rdr_titles(n)


# ── 2. no cross-project leakage: the project comes from the document's owner ─────────────────


def test_art_rdr_084_never_pulls_nexus_rdr_084s_records() -> None:
    stream, t2, _ = _read(_resolution(_src()))
    blob = "\n".join(r.text for r in stream.records)
    assert "NEXUS" not in blob and "NEX" not in blob and "BLOCKED" not in blob
    assert {p for p, _ in t2.gets} == {"ART_rdr"}, "no read ever names another project"


def test_the_same_number_under_the_nexus_owner_reads_only_the_nexus_project() -> None:
    stream, t2, _ = _read(_resolution(_src(tumbler=NEXUS_TUMBLER)))
    assert [r.text for r in stream.records] == [NEXUS_META.strip(), NEXUS_GATE.strip()]
    assert {p for p, _ in t2.gets} == {"nexus_rdr"}


def test_an_art_source_with_no_art_records_gets_none_not_the_other_projects() -> None:
    rows = {("nexus_rdr", "084"): NEXUS_META, ("nexus_rdr", "084-gate-latest"): NEXUS_GATE}
    stream, t2, _ = _read(_resolution(_src()), t2=FakeT2(rows))
    assert stream.records == ()
    (outcome,) = stream.sources
    assert outcome.status == "no_records" and "ART_rdr" in outcome.detail
    assert {p for p, _ in t2.gets} == {"ART_rdr"}


# ── 3. who gets no stream, and the envelope says why ─────────────────────────────────────────


def test_a_non_rdr_source_gets_no_stream_and_no_read() -> None:
    stream, t2, cat = _read(_resolution(_src("1.2.9000", rdr_number=None)))
    assert stream.records == () and stream.reserved_items() == ()
    assert stream.sources[0].status == "not_rdr"
    assert t2.gets == [] and cat.calls == [], "a non-RDR source costs no catalog call and no T2 read"


@pytest.mark.parametrize(
    "status", ["candidates", "unresolved"],
)
def test_an_ambiguous_or_unresolved_source_gets_no_stream_and_says_why(status: str) -> None:
    detail = "2 documents carry the number RDR-084; name the document by its tumbler"
    stream, t2, cat = _read(_resolution(_src(status=status, detail=detail)))
    assert stream.records == () and t2.gets == [] and cat.calls == []
    (outcome,) = stream.sources
    assert outcome.status == "not_resolved" and status in outcome.detail and detail in outcome.detail
    wire = stream.to_wire()
    assert wire["sources"][0]["status"] == "not_resolved" and wire["records"] == []


@pytest.mark.parametrize(
    ("tumbler", "why"),
    [("1.7.5", "not in the catalog"), ("1.9.5", "curator"), ("1.8.5", "no name")],
)
def test_an_owner_that_names_no_repo_project_gets_no_stream(tumbler: str, why: str) -> None:
    stream, t2, _ = _read(_resolution(_src(tumbler=tumbler)))
    assert stream.records == () and t2.gets == []
    (outcome,) = stream.sources
    assert outcome.status == "project_undetermined" and why in outcome.detail
    assert outcome.project == ""
    assert any("project" in line for line in stream.disclosures())


def test_no_catalog_means_the_project_is_undetermined_not_guessed() -> None:
    t2 = FakeT2(_rows())
    stream = read_lifecycle_stream(_resolution(_src()), cat=None, t2_get=t2.get)
    assert stream is not None and stream.records == () and t2.gets == []
    assert stream.sources[0].status == "project_undetermined"


def test_an_owner_probe_that_raises_is_disclosed_not_raised() -> None:
    cat = MagicMock()
    cat.get_owner_by_prefix.side_effect = RuntimeError("catalog down")
    stream, t2, _ = _read(_resolution(_src()), cat=cat)
    assert stream.records == () and t2.gets == []
    assert stream.sources[0].status == "project_undetermined" and "RuntimeError" in stream.sources[0].detail


def test_no_resolution_is_no_stream() -> None:
    assert read_lifecycle_stream(None, cat=FakeCatalog({}), t2_get=FakeT2({}).get) is None


def test_a_t2_failure_is_disclosed_and_not_retried_for_later_sources() -> None:
    t2 = FakeT2(_rows(), fail_on={"ART_rdr"})
    stream, _, _ = _read(_resolution(_src(), _src("RDR-085")), t2=t2)
    assert stream.records == ()
    assert [o.status for o in stream.sources] == ["read_failed", "read_failed"]
    assert len(t2.gets) == 1, "after the first failure T2 is not asked again in this call"
    assert any("ConnectionError" in line for line in stream.disclosures())


# ── 4. caps: records read, characters read, records admitted ─────────────────────────────────


def test_the_caps_are_stated_in_one_object_and_on_the_wire() -> None:
    caps = DEFAULT_LIFECYCLE_CAPS
    assert caps.max_records >= 2 and caps.max_reads >= 8
    assert caps.max_records < RESERVED_SHARE_MAX, "records are a small part of the reserved share"
    stream, _, _ = _read(_resolution(_src()))
    assert stream.to_wire()["caps"] == caps.to_wire()
    assert set(caps.to_wire()) == {
        "max_records", "max_reads", "max_record_chars", "max_chars", "max_seconds", "call_timeout_seconds"}


def test_a_record_over_the_character_cap_is_cut_and_disclosed() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_record_chars=100)
    rows = {("ART_rdr", "084"): "x" * 5000, ("ART_rdr", "084-gate-latest"): ART_GATE}
    stream, _, _ = _read(_resolution(_src()), t2=FakeT2(rows), caps=caps)
    meta, gate = stream.records
    assert meta.truncated is True and meta.chars_full == 5000 and len(meta.text) <= 100 + 80
    assert "truncated" in meta.text and gate.truncated is False
    assert any("5000" in line and "100" in line for line in stream.disclosures())
    assert stream.chars <= 2 * (100 + 80)


def test_total_characters_are_capped_and_the_rest_are_not_admitted() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_record_chars=400, max_chars=500)
    rows = {("ART_rdr", "084"): "m" * 400, ("ART_rdr", "084-gate-latest"): "g" * 400}
    stream, _, _ = _read(_resolution(_src()), t2=FakeT2(rows), caps=caps)
    assert len(stream.records) == 2
    assert sum(len(r.text) for r in stream.records) <= 500 + 2 * 80, "the second record is cut to what remains"
    assert "max_chars" in stream.caps_hit
    assert any("max_chars" in line for line in stream.disclosures())


def test_records_admitted_are_capped_and_later_sources_are_disclosed() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_records=2)
    rows = {(p, t): c for n in (84, 85, 86) for p, t, c in (
        ("ART_rdr", f"{n:03d}", f"meta {n}"), ("ART_rdr", f"{n:03d}-gate-latest", f"gate {n}"))}
    stream, t2, _ = _read(_resolution(_src("RDR-084"), _src("RDR-085", rdr_number=85), _src("RDR-086", rdr_number=86)),
                          t2=FakeT2(rows), caps=caps)
    assert len(stream.records) == 2 and {r.text for r in stream.records} == {"meta 84", "gate 84"}
    assert [o.status for o in stream.sources] == ["read", "over_cap", "over_cap"]
    assert "max_records" in stream.caps_hit
    assert not any("85" in t for _, t in t2.gets), "a source past the cap costs no read"


def test_the_record_cap_holds_inside_one_source_too() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_records=1)
    stream, _, _ = _read(_resolution(_src()), caps=caps)
    assert [r.kind for r in stream.records] == ["metadata"], "the gate record is not admitted past the cap"
    assert "max_records" in stream.caps_hit and stream.sources[0].status == "read"
    assert any("max_records" in line for line in stream.disclosures()), (
        "a cut inside a source that stays 'read' is still told to the reader")


def test_reads_are_capped_whatever_the_sources_hold() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_reads=5)
    sources = [_src(f"RDR-{n:03d}", rdr_number=n) for n in range(10, 16)]
    stream, t2, _ = _read(_resolution(*sources), t2=FakeT2({}), caps=caps)
    assert len(t2.gets) <= 5
    assert "max_reads" in stream.caps_hit
    assert [o.status for o in stream.sources].count("over_cap") >= 1


def test_one_source_with_no_records_reads_at_most_eight_titles() -> None:
    stream, t2, _ = _read(_resolution(_src()), t2=FakeT2({}))
    assert len(t2.gets) == 8 and stream.sources[0].status == "no_records"
    assert all(g[0] == "ART_rdr" for g in t2.gets)


def test_an_empty_record_body_is_not_a_record() -> None:
    rows = {("ART_rdr", "084"): "   \n", ("ART_rdr", "084-gate-latest"): ART_GATE}
    stream, _, _ = _read(_resolution(_src()), t2=FakeT2(rows))
    assert [r.kind for r in stream.records] == ["gate-latest"]


# ── 4b. the time budget: the reads stop at the deadline and say so ────────────────────────────


def test_the_deadline_stops_the_reads_and_shrinks_each_call_timeout() -> None:
    clock = FakeClock()
    t2 = FakeT2({}, clock=clock, cost=1.0)
    stream, _, _ = _read(
        _resolution(_src(), _src("RDR-085", rdr_number=85)), t2=t2, deadline=clock() + 2.6, clock=clock)
    assert len(t2.gets) == 3, "reads at 2.6, 1.6 and 0.6 seconds left; the fourth would be past the deadline"
    assert t2.timeouts == pytest.approx([2.6, 1.6, 0.6]), "each call gets what is left, never the client default"
    assert "deadline" in stream.caps_hit
    assert [o.status for o in stream.sources] == ["read_capped", "over_cap"], (
        "the lookup the deadline cut is not 'no_records', and the source after it is not read at all")
    assert any("deadline" in line for line in stream.disclosures())


def test_the_deadline_is_met_by_a_slow_t2_not_by_the_client_default() -> None:
    clock = FakeClock()
    t2 = FakeT2({}, clock=clock, cost=40.0)  # each read burns far more than the budget
    stream, _, _ = _read(_resolution(_src()), t2=t2, deadline=clock() + 5.0, clock=clock)
    assert len(t2.gets) == 1 and "deadline" in stream.caps_hit and t2.timeouts[0] <= 5.0


def test_without_a_deadline_the_fixed_ceiling_bounds_the_run_and_each_call() -> None:
    clock = FakeClock()
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_seconds=2.6)
    t2 = FakeT2({}, clock=clock, cost=1.0)
    stream, _, _ = _read(_resolution(_src()), t2=t2, caps=caps, clock=clock)
    assert len(t2.gets) == 3 and "deadline" in stream.caps_hit
    clock2 = FakeClock()
    t2b = FakeT2({}, clock=clock2, cost=0.0)
    _read(_resolution(_src()), t2=t2b, clock=clock2)
    assert max(t2b.timeouts) <= DEFAULT_LIFECYCLE_CAPS.call_timeout_seconds, "no call outlives the per-call ceiling"


def test_a_deadline_already_past_reads_nothing() -> None:
    clock = FakeClock()
    t2 = FakeT2(_rows(), clock=clock)
    stream, _, _ = _read(_resolution(_src()), t2=t2, deadline=clock() - 1.0, clock=clock)
    assert t2.gets == [] and stream.records == () and "deadline" in stream.caps_hit
    assert stream.sources[0].status == "over_cap"


# ── 4c. every cap that cut something is told to the reader ───────────────────────────────────


def _capped_streams() -> dict[str, LifecycleStream]:
    clock = FakeClock()
    out: dict[str, LifecycleStream] = {}
    out["max_records"], _, _ = _read(_resolution(_src()), caps=replace(DEFAULT_LIFECYCLE_CAPS, max_records=1))
    out["max_reads"], _, _ = _read(_resolution(_src()), caps=replace(DEFAULT_LIFECYCLE_CAPS, max_reads=1))
    out["max_chars"], _, _ = _read(
        _resolution(_src()), t2=FakeT2({("ART_rdr", "084"): "m" * 400, ("ART_rdr", "084-gate-latest"): "g" * 400}),
        caps=replace(DEFAULT_LIFECYCLE_CAPS, max_record_chars=400, max_chars=500))
    out["deadline"], _, _ = _read(
        _resolution(_src()), t2=FakeT2(_rows(), clock=clock, cost=1.0), deadline=clock() + 1.5, clock=clock)
    return out


@pytest.mark.parametrize("cap", ["max_records", "max_reads", "max_chars", "deadline"])
def test_every_cap_that_cut_something_has_a_text_line(cap: str) -> None:
    stream = _capped_streams()[cap]
    assert cap in stream.caps_hit, f"the scenario must hit {cap}: {stream.caps_hit}"
    assert any(cap in line for line in stream.disclosures()), (cap, stream.disclosures())


def test_a_lookup_cut_by_a_cap_is_read_capped_never_no_records() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_reads=3)
    stream, t2, _ = _read(_resolution(_src()), t2=FakeT2({}), caps=caps)
    assert len(t2.gets) == 3 and "max_reads" in stream.caps_hit
    (outcome,) = stream.sources
    assert outcome.status == "read_capped", "three of the four titles were asked; the record may exist under the fourth"
    assert any("max_reads" in line for line in stream.disclosures())
    assert not any("no T2 lifecycle records" in line for line in stream.disclosures())


def test_a_source_that_is_not_an_rdr_gets_a_one_line_notice() -> None:
    stream, t2, _ = _read(_resolution(_src("1.2.50", rdr_number=None)))
    assert t2.gets == [] and stream.sources[0].status == "not_rdr"
    assert [line for line in stream.disclosures() if "1.2.50" in line and "not an RDR" in line]


def test_metadata_without_a_gate_record_says_the_gate_record_is_absent() -> None:
    stream, _, _ = _read(_resolution(_src()), t2=FakeT2({("ART_rdr", "084"): ART_META}))
    assert [r.kind for r in stream.records] == ["metadata"] and stream.sources[0].status == "read"
    assert any("no latest gate record" in line and "ART_rdr" in line for line in stream.disclosures())


def test_a_gate_record_without_metadata_says_the_metadata_record_is_absent() -> None:
    stream, _, _ = _read(_resolution(_src()), t2=FakeT2({("ART_rdr", "084-gate-latest"): ART_GATE}))
    assert [r.kind for r in stream.records] == ["gate-latest"]
    assert any("no metadata record" in line for line in stream.disclosures())


def test_both_records_present_has_no_absence_notice() -> None:
    stream, _, _ = _read(_resolution(_src()))
    assert stream.disclosures() == []


# ── 4d. the same RDR named twice is read and admitted once ───────────────────────────────────


def test_the_same_rdr_named_by_id_and_by_tumbler_is_read_once() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_records=2)
    rows = {**_rows(), ("ART_rdr", "085"): "meta 85", ("ART_rdr", "085-gate-latest"): "gate 85"}
    stream, t2, _ = _read(
        _resolution(_src("RDR-084"), _src(ART_TUMBLER), _src("RDR-085", rdr_number=85)), t2=FakeT2(rows), caps=caps)
    assert [r.text for r in stream.records] == [ART_META.strip(), ART_GATE.strip()]
    assert [o.status for o in stream.sources][:2] == ["read", "duplicate"]
    assert len(t2.gets) == 3, "the second naming costs no T2 read"
    assert "max_records" in stream.caps_hit and stream.sources[2].status == "over_cap", (
        "the cap is spent on distinct records only: the third source finds it spent, not the duplicate")
    stream2, _, _ = _read(
        _resolution(_src("RDR-084"), _src(ART_TUMBLER), _src("RDR-085", rdr_number=85)), t2=FakeT2(rows))
    assert len(stream2.records) == 4 and stream2.sources[2].status == "read"
    assert len({(r.project, r.title) for r in stream2.records}) == 4


# ── 4d2. the project-naming rule is the one the RDR tooling and the indexer share ────────────


def _run_git(*args: str) -> None:
    import subprocess

    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def test_the_gate_project_name_and_the_registered_owner_name_agree_for_a_main_checkout_and_a_worktree(tmp_path) -> None:
    """``_project_for`` reads ``<owner name>_rdr``. The RDR tooling writes ``_gate_repo_name(root)`` and the
    indexer registers the owner as ``_repo_identity(repo)[0]``; if they ever diverge, every lifecycle read
    addresses a project nothing was written to. Pinned for the main checkout and a linked worktree."""
    from nexus.commands.rdr import _gate_repo_name
    from nexus.repo_identity import _repo_identity, _repo_identity_with_main

    main = tmp_path / "lifecycle-pin-repo"
    main.mkdir()
    _run_git("-C", str(main), "init", "-q")
    _run_git("-C", str(main), "commit", "-q", "--allow-empty", "-m", "x")
    linked = tmp_path / "linked-worktree"
    _run_git("-C", str(main), "worktree", "add", "-q", "-b", "feature/x", str(linked))
    names = {
        "gate main": _gate_repo_name(str(main)), "gate worktree": _gate_repo_name(str(linked)),
        "indexer main": _repo_identity(main)[0], "indexer worktree": _repo_identity(linked)[0],
        "indexer with-main worktree": _repo_identity_with_main(linked)[0],
    }
    assert set(names.values()) == {"lifecycle-pin-repo"}, names


def test_the_indexer_registers_the_owner_under_the_repo_identity_name() -> None:
    import inspect

    from nexus import indexer

    src = inspect.getsource(indexer)
    assert "_repo_basename, _repo_hash = _repo_identity(repo)" in src and "repo_name=_repo_basename," in src
    assert 'name=repo_name,\n                owner_type="repo"' in src, (
        "the catalog hook registers the repo owner under its repo_name argument")


# ── 4e. the reserved tokens: none is a prefix of another ─────────────────────────────────────


def test_no_reserved_token_is_a_prefix_of_another() -> None:
    stream = _lifecycle_stream(3)
    tokens = [i.token for i in stream.items]
    assert len(set(tokens)) == len(tokens)
    for a in tokens:
        assert not any(b != a and b.startswith(a) for b in tokens), (a, tokens)


def test_a_rank_that_drops_the_metadata_record_and_keeps_the_gate_record_counts_one_drop() -> None:
    stream = _lifecycle_stream(0)
    assert [i.raw for i in stream.items] == [ART_META.strip(), ART_GATE.strip()]
    _hydrate("rank", {"inputs": ["s"], "criterion": "c"}, stream)
    survivors = [stream.items[1].text]  # the rank kept the gate record only
    with reserved_scope(stream):
        set_reserved_step(1, feeds_from=("operator_rank",))
        _hydrate_operator_args("generate", {"context": survivors, "instruction": "i"})
    assert stream.report()["dropped_before_terminal"] == {"step": 1, "missing": 1, "of": 2}
    assert stream.is_primary_text(survivors[0]) and not stream.is_primary_text("unrelated")


# ── 5. inside the reserved share: truncation and the operator cap never evict a record ───────


def _chunk(i: int) -> SelectedChunk:
    return SelectedChunk(
        source="RDR-084", doc_id="1.2.3", position=i, chash=f"{i:02x}" * 32, collection="rdr__x",
        label=f"Section {i}", label_source="content", text=f"PRIMARY TEXT {i}",
    )


def _lifecycle_stream(n_chunks: int) -> ReservedStream:
    life, _, _ = _read(_resolution(_src()))
    stream = ReservedStream.from_evidence(
        PrimaryEvidence(items=tuple(_chunk(i) for i in range(n_chunks))), extra=life.reserved_items())
    assert stream is not None
    assert [i.raw for i in stream.items[:2]] == [ART_META.strip(), ART_GATE.strip()], "the helper's own non-vacuity check"
    return stream


def _hydrate(tool: str, args: dict, stream: ReservedStream):
    with reserved_scope(stream):
        set_reserved_step(0)
        return _hydrate_operator_args(tool, args)


def test_the_share_cap_evicts_chunks_never_the_t2_records() -> None:
    stream = _lifecycle_stream(RESERVED_SHARE_MAX + 10)
    assert len(stream.items) == RESERVED_SHARE_MAX
    assert [i.raw for i in stream.items[:2]] == [ART_META.strip(), ART_GATE.strip()]
    assert stream.over_share_dropped == 12


def test_with_inputs_over_the_operator_cap_both_records_reach_the_operator_in_explicit_inputs() -> None:
    stream = _lifecycle_stream(10)
    secondary = [f"secondary {k}" for k in range(150)]
    _, args = _hydrate("rank", {"inputs": secondary, "criterion": "c"}, stream)
    items = json.loads(args["items"])
    assert len(items) == _OPERATOR_MAX_INPUTS
    assert items[:2] == [i.text for i in stream.items[:2]] and "ART gate record" in items[1]
    assert items[-1] == f"secondary {_OPERATOR_MAX_INPUTS - len(stream.items) - 1}", "only secondary items were cut"


def test_with_ids_over_the_operator_cap_both_records_reach_the_operator() -> None:
    stream = _lifecycle_stream(10)
    ids = [f"{0xA000 + i:064x}" for i in range(150)]

    def fake(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": [f"secondary {k}" for k, _ in enumerate(ids)]}

    with patch("nexus.mcp.core.store_get_many", side_effect=fake), \
         patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        _, args = _hydrate("rank", {"ids": ids, "criterion": "c"}, stream)
    items = json.loads(args["items"])
    assert len(items) == _OPERATOR_MAX_INPUTS
    assert items[:2] == [i.text for i in stream.items[:2]] and "ART gate record" in items[1]
    assert stream.report()["secondary_cut"] == 150 + len(stream.items) - _OPERATOR_MAX_INPUTS


def test_a_records_only_stream_is_built_when_the_document_yields_no_chunks() -> None:
    life, _, _ = _read(_resolution(_src()))
    stream = ReservedStream.from_evidence(None, extra=life.reserved_items())
    assert stream is not None and len(stream.items) == 2


# ── 6. nx_answer wiring ──────────────────────────────────────────────────────────────────────


def _memory_db(fake: FakeT2, *, delay: float = 0.0) -> MagicMock:
    """A T2 stand-in whose ``memory.get`` is the fake's; everything wider raises."""
    db = MagicMock()
    kwargs: list[dict] = []

    def _get(project=None, title=None, id=None, *, timeout=None, retry_read_timeout=True):  # noqa: A002
        kwargs.append({"timeout": timeout, "retry_read_timeout": retry_read_timeout})
        if delay:
            time.sleep(delay)
        return fake.get(project, title, timeout)

    db.memory_get_kwargs = kwargs
    db.memory.get.side_effect = _get
    for banned in ("search", "list_entries", "get_all", "resolve_title", "get_projects_with_prefix"):
        getattr(db.memory, banned).side_effect = AssertionError(f"unbounded T2 read: {banned}")
        getattr(db, banned).side_effect = AssertionError(f"unbounded T2 read: {banned}")
    db.get.side_effect = AssertionError("the facade get has no timeout: read through db.memory")
    return db


@pytest.mark.asyncio
async def test_nx_answer_lifecycle_reads_exact_titles_through_the_shared_t2_transport_and_nothing_wider() -> None:
    from nexus.mcp import core as mcp_core

    fake = FakeT2(_rows())
    db = _memory_db(fake)
    cat = FakeCatalog(_owners())
    with patch.object(mcp_core, "_t2_ctx", side_effect=AssertionError("a fresh T2Database per call")), \
         patch.object(mcp_core, "_t2_index_write", lambda fn, **_kw: fn(db)), \
         patch.object(mcp_core, "_get_catalog", return_value=cat):
        life = await mcp_core._nx_answer_lifecycle_stream(_resolution(_src()))
    assert life is not None and [r.kind for r in life.records] == ["metadata", "gate-latest"]
    assert fake.gets == [("ART_rdr", "84"), ("ART_rdr", "084"), ("ART_rdr", "084-gate-latest")]
    assert all(k["retry_read_timeout"] is False and 0 < k["timeout"] <= DEFAULT_LIFECYCLE_CAPS.call_timeout_seconds
               for k in db.memory_get_kwargs), "a clamped timeout is never retried at the same clamp"


@pytest.mark.asyncio
async def test_nx_answer_lifecycle_is_none_without_a_named_source_and_never_opens_t2() -> None:
    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "_t2_ctx") as ctx, patch.object(mcp_core, "_t2_index_write") as write:
        assert await mcp_core._nx_answer_lifecycle_stream(None) is None
    ctx.assert_not_called()
    write.assert_not_called()


@pytest.mark.asyncio
async def test_nx_answer_lifecycle_survives_an_unopenable_t2() -> None:
    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "_t2_index_write", side_effect=ConnectionError("down")), \
         patch.object(mcp_core, "_get_catalog", return_value=FakeCatalog(_owners())):
        life = await mcp_core._nx_answer_lifecycle_stream(_resolution(_src()))
    assert life is not None and life.records == ()
    assert life.sources[0].status == "read_failed" and "ConnectionError" in life.sources[0].detail


@pytest.mark.asyncio
async def test_nx_answer_lifecycle_honours_the_answer_deadline_against_a_slow_t2() -> None:
    from nexus.mcp import core as mcp_core

    fake = FakeT2({})
    db = _memory_db(fake, delay=0.2)
    t0 = time.monotonic()
    with patch.object(mcp_core, "_t2_index_write", lambda fn, **_kw: fn(db)), \
         patch.object(mcp_core, "_get_catalog", return_value=FakeCatalog(_owners())):
        life = await mcp_core._nx_answer_lifecycle_stream(
            _resolution(_src(), _src("RDR-085", rdr_number=85)), deadline=t0 + 0.5)
    elapsed = time.monotonic() - t0
    assert life is not None and "deadline" in life.caps_hit
    assert elapsed < 1.2, f"16 sequential reads of 0.2 s would take 3.2 s; the deadline stopped them at {elapsed:.2f}s"
    assert 1 <= len(fake.gets) <= 4 and life.sources[0].status == "read_capped"


@pytest.mark.asyncio
async def test_the_primary_stream_puts_the_records_first_and_survives_a_failed_chunk_read() -> None:
    from nexus.mcp import core as mcp_core

    life, _, _ = _read(_resolution(_src()))
    resolution = _resolution(_src())
    evidence = PrimaryEvidence(items=tuple(_chunk(i) for i in range(3)))
    with patch("nexus.plans.primary_evidence.select_primary_evidence", return_value=evidence), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch.object(mcp_core, "_get_t3", return_value=MagicMock()), \
         patch.object(mcp_core, "_get_catalog", return_value=None):
        ev, stream = await mcp_core._nx_answer_primary_stream(resolution, life)
        assert ev is evidence and stream is not None
        assert [i.raw for i in stream.items[:2]] == [ART_META.strip(), ART_GATE.strip()] and len(stream.items) == 5
    with patch("nexus.plans.primary_evidence.select_primary_evidence", side_effect=RuntimeError("t3 down")), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch.object(mcp_core, "_get_t3", return_value=MagicMock()), \
         patch.object(mcp_core, "_get_catalog", return_value=None):
        ev, stream = await mcp_core._nx_answer_primary_stream(resolution, life)
    # nexus-jypmj.1.9: a failed read is disclosed on the evidence (error), not returned as "no evidence".
    assert ev is not None and ev.error == "RuntimeError" and ev.items == ()
    assert stream is not None and len(stream.items) == 2, "the T2 records do not depend on T3"


def test_without_lifecycle_the_primary_stream_call_is_unchanged() -> None:
    import asyncio

    from nexus.mcp import core as mcp_core

    assert asyncio.run(mcp_core._nx_answer_primary_stream(None)) == (None, None)
    assert asyncio.run(mcp_core._nx_answer_primary_stream(_resolution(_src(status="unresolved")))) == (None, None)


#: A question with a gate facet: from Phase 2 (nexus-jypmj.2.2) the T2 stream is read only for such a request.
_GATE_QUESTION = "State the gate history of RDR-084."


def _drive_envelope(resolution, life):
    from unittest.mock import AsyncMock

    from tests.test_rdr232_caller_scope import _drive_nx_answer
    from tests.test_rdr232_p1_review_fixes import _MULTI_PLAN, _plan_run_result

    run = AsyncMock(return_value=_plan_run_result([{"text": "answer"}], []))
    with patch("nexus.mcp.core._nx_answer_named_sources", return_value=resolution), \
         patch("nexus.mcp.core._nx_answer_lifecycle_stream", AsyncMock(return_value=life)), \
         patch("nexus.mcp.core._nx_answer_primary_stream", AsyncMock(return_value=(None, None))), \
         patch("nexus.plans.runner.plan_run", run):
        env, _ = _drive_nx_answer(
            _MULTI_PLAN, scope="", structured=True, bindings={"source": "RDR-084"}, question=_GATE_QUESTION)
        text, _ = _drive_nx_answer(
            _MULTI_PLAN, scope="", structured=False, bindings={"source": "RDR-084"}, question=_GATE_QUESTION)
    return env, text


def test_the_envelope_carries_the_lifecycle_report_and_text_mode_the_disclosure() -> None:
    caps = replace(DEFAULT_LIFECYCLE_CAPS, max_record_chars=60)
    rows = {("ART_rdr", "084"): "y" * 500, ("ART_rdr", "084-gate-latest"): ART_GATE}
    life, _, _ = _read(_resolution(_src()), t2=FakeT2(rows), caps=caps)
    env, text = _drive_envelope(_resolution(_src()), life)
    wire = env["lifecycle_stream"]
    assert wire["caps"] == caps.to_wire() and [r["kind"] for r in wire["records"]] == ["metadata", "gate-latest"]
    assert wire["records"][0]["truncated"] is True and "text" not in wire["records"][0], "the body is evidence, not disclosure"
    assert wire["sources"][0]["status"] == "read" and wire["sources"][0]["project"] == "ART_rdr"
    assert "truncated" in text and "[evidence request:" in text


def test_the_envelope_key_is_none_when_no_source_was_named() -> None:
    env, _ = _drive_envelope(None, None)
    assert env["lifecycle_stream"] is None
