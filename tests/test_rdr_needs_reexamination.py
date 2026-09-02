# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-201 P3.3 (nexus-j9z30.22): ``nx rdr set-status`` marks dependents
``needs-reexamination``; ``rdr-audit`` lists the markers.

Rulings (Sam, 2026-09-02): the walk follows ONLY ``supersedes`` catalog
edges -- never ``relates`` (259 of 265 live edges, free-text
``related_rdrs`` reading aids) and never ``parent_rdr`` -- and ONE
direction, successor -> predecessor: a successor's flip marks the records
it supersedes (their ``superseded`` verdict rests on it); a predecessor's
flip marks nobody. Report-only posture (RDR-081 precedent): the marker is appended to
the dependent's T2 entry and surfaced by the audit, never auto-resolved and
never used to block a flip. A catalog or T2 failure is printed as a note;
the flip itself has already happened and stays.

Test doubles: the catalog reader seam ``_catalog_reader_factory`` and the
repo-scope seam ``_rdr_repo_scope`` (owner tumbler + source prefix, which
production derives from git identity) are monkeypatched; the T2 seam is
``_t2_client_factory``, as in test_rdr_set_status.py. Resolution itself is
NOT stubbed -- the fake catalog serves real ``CatalogEntry`` /
``CatalogLink`` objects and the production ``rdr_resolution`` /
``is_in_repo`` / ``resolve_all`` chain runs over them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import nexus.commands.rdr as rdr_mod
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, CatalogLink
from nexus.commands.rdr import rdr

_OWNER = Tumbler.parse("1.7")


def _rdr_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_rdr(rdr_dir: Path, num: int, status: str) -> Path:
    path = rdr_dir / f"rdr-{num:03d}-thing-{num}.md"
    path.write_text(
        f"---\nid: RDR-{num:03d}\ntitle: Thing {num}\nstatus: {status}\n"
        f"accepted_date: 2026-01-01\n---\n\n## Problem\n\nProse.\n",
        encoding="utf-8",
    )
    return path


def _entry(tmp_path: Path, num: int, leaf: int) -> CatalogEntry:
    rel = f"docs/rdr/rdr-{num:03d}-thing-{num}.md"
    return CatalogEntry(
        tumbler=Tumbler.parse(f"1.7.{leaf}"), title=f"RDR-{num:03d}: Thing {num}", author="",
        year=2026, content_type="rdr", file_path=rel, corpus="rdr", physical_collection="",
        chunk_count=1, head_hash="", indexed_at="", source_uri=f"file://{tmp_path}/{rel}",
    )


def _link(from_t: str, to_t: str, link_type: str) -> CatalogLink:
    return CatalogLink(
        from_tumbler=Tumbler.parse(from_t), to_tumbler=Tumbler.parse(to_t), link_type=link_type,
        from_span="", to_span="", created_by="rdr_dependency_extractor", created_at="",
    )


@dataclass
class _FakeCatalog:
    entries: list[CatalogEntry]
    links: list[CatalogLink] = field(default_factory=list)

    def all_documents(self, content_type: str | None = None) -> list[CatalogEntry]:
        return [e for e in self.entries if content_type is None or e.content_type == content_type]

    def resolve_path(self, tumbler: object) -> Path | None:
        return None  # only consulted to disambiguate colliding numbers; none here

    def links_from(self, tumbler: object, link_type: str = "", link_types: list[str] | None = None) -> list[CatalogLink]:
        return [l for l in self.links if str(l.from_tumbler) == str(tumbler) and (not link_type or l.link_type == link_type)]

    def links_to(self, tumbler: object, link_type: str = "", link_types: list[str] | None = None) -> list[CatalogLink]:
        return [l for l in self.links if str(l.to_tumbler) == str(tumbler) and (not link_type or l.link_type == link_type)]


class _FakeT2Client:
    def __init__(self, entries: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.entries = dict(entries or {})
        self.puts: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeT2Client":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def get(self, project: str | None = None, title: str | None = None, id: int | None = None) -> dict[str, Any] | None:
        return self.entries.get((project, title))

    def put(self, project: str, title: str, content: str, tags: str = "", ttl: int | None = 30, **kw: Any) -> int:
        self.puts.append({"project": project, "title": title, "content": content, "tags": tags, "ttl": ttl})
        self.entries[(project, title)] = {"title": title, "content": content, "tags": tags}
        return len(self.puts)

    def get_all(self, project: str) -> list[dict[str, Any]]:
        return [dict(v, title=t) for (p, t), v in self.entries.items() if p == project]


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cat: _FakeCatalog, t2: _FakeT2Client) -> None:
    monkeypatch.setattr(rdr_mod, "_catalog_reader_factory", lambda: cat)
    monkeypatch.setattr(rdr_mod, "_rdr_repo_scope", lambda _cat, root: (_OWNER, f"file://{Path(root)}/docs/rdr/"))
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: t2)


def _project(tmp_path: Path) -> str:
    return f"{tmp_path.name}_rdr"


def _flip(tmp_path: Path, num: int, new_status: str):
    return CliRunner().invoke(rdr, ["set-status", str(num), new_status, "--root", str(tmp_path)])


# ---------------------------------------------------------------------------
# The ruling's own test: supersedes marks, relates does not
# ---------------------------------------------------------------------------


def test_supersedes_dependent_is_marked_and_relates_dependent_is_not(tmp_path, monkeypatch):
    """A record with BOTH a supersedes dependent and a relates dependent
    produces exactly ONE marker (ruling nexus-j9z30.22). If a future
    session widens the walk, this is the test it has to argue with."""
    rdr_dir = _rdr_dir(tmp_path)
    for n in (14, 15, 16):
        _write_rdr(rdr_dir, n, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2), _entry(tmp_path, 16, 3)],
        links=[_link("1.7.2", "1.7.1", "supersedes"), _link("1.7.2", "1.7.3", "relates")],
    )
    project = _project(tmp_path)
    t2 = _FakeT2Client({
        (project, "14"): {"content": "status: superseded\ntitle: Fourteen\n", "tags": "rdr"},
        (project, "16"): {"content": "status: accepted\ntitle: Sixteen\n", "tags": "rdr"},
    })
    _install(monkeypatch, tmp_path, cat, t2)

    result = _flip(tmp_path, 15, "abandoned")
    assert result.exit_code == 0, result.output
    assert [p["title"] for p in t2.puts] == ["14"]
    assert t2.puts[0]["content"].endswith("needs-reexamination: RDR-15 accepted->abandoned (RDR-15 supersedes RDR-14)\n")
    assert t2.puts[0]["content"].startswith("status: superseded\ntitle: Fourteen\n")
    assert f"marked {project}/14 needs-reexamination" in result.output


def test_only_the_predecessor_is_marked_never_the_successor(tmp_path, monkeypatch):
    """RDR-15 supersedes RDR-14 and is superseded by RDR-16: flipping 15
    marks 14 only (ruling: successor -> predecessor). 16's verdict does not
    rest on 15; 14's ``superseded`` does."""
    rdr_dir = _rdr_dir(tmp_path)
    for n in (14, 15, 16):
        _write_rdr(rdr_dir, n, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2), _entry(tmp_path, 16, 3)],
        links=[_link("1.7.2", "1.7.1", "supersedes"), _link("1.7.3", "1.7.2", "supersedes")],
    )
    project = _project(tmp_path)
    t2 = _FakeT2Client({
        (project, "14"): {"content": "status: superseded\n", "tags": ""},
        (project, "RDR-16"): {"content": "status: accepted\n", "tags": "rdr,x"},
    })
    _install(monkeypatch, tmp_path, cat, t2)

    result = _flip(tmp_path, 15, "abandoned")
    assert result.exit_code == 0, result.output
    assert [p["title"] for p in t2.puts] == ["14"]
    assert t2.puts[0]["content"].endswith("needs-reexamination: RDR-15 accepted->abandoned (RDR-15 supersedes RDR-14)\n")


def test_flipping_a_predecessor_marks_nobody(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 15, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2)],
        links=[_link("1.7.2", "1.7.1", "supersedes")],
    )
    t2 = _FakeT2Client({(_project(tmp_path), "15"): {"content": "status: accepted\n", "tags": ""}})
    _install(monkeypatch, tmp_path, cat, t2)
    assert _flip(tmp_path, 14, "closed").exit_code == 0
    assert t2.puts == []


def test_marker_put_keeps_the_entry_permanent_and_keeps_its_tags(tmp_path, monkeypatch):
    """The T2 facade's put() defaults ttl to 30 days -- a marker write that
    forgot ttl=None would expire a permanent RDR record; one that passed
    tags="" would strip them."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 15, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2)],
        links=[_link("1.7.2", "1.7.1", "supersedes")],
    )
    project = _project(tmp_path)
    t2 = _FakeT2Client({(project, "14"): {"content": "status: superseded\n", "tags": "rdr,architecture"}})
    _install(monkeypatch, tmp_path, cat, t2)

    assert _flip(tmp_path, 15, "closed").exit_code == 0
    assert t2.puts == [{
        "project": project, "title": "14", "tags": "rdr,architecture", "ttl": None,
        "content": "status: superseded\nneeds-reexamination: RDR-15 accepted->closed (RDR-15 supersedes RDR-14)\n",
    }]


def test_no_supersedes_neighbour_writes_nothing(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 16, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 16, 3)],
        links=[_link("1.7.3", "1.7.1", "relates")],
    )
    t2 = _FakeT2Client({(_project(tmp_path), "16"): {"content": "status: accepted\n", "tags": ""}})
    _install(monkeypatch, tmp_path, cat, t2)

    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert t2.puts == []
    assert "needs-reexamination" not in result.output


def test_dependent_with_no_t2_entry_is_named_not_invented(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 15, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2)],
        links=[_link("1.7.1", "1.7.2", "supersedes")],
    )
    t2 = _FakeT2Client({})
    _install(monkeypatch, tmp_path, cat, t2)

    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert t2.puts == []
    assert "no T2 entry" in result.output and "15" in result.output


def test_catalog_failure_never_blocks_the_flip(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    path = _write_rdr(rdr_dir, 14, "accepted")

    def _boom():
        raise RuntimeError("catalog service unreachable")

    monkeypatch.setattr(rdr_mod, "_catalog_reader_factory", _boom)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _FakeT2Client())

    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert "status: closed" in path.read_text(encoding="utf-8")
    assert "dependents not marked" in result.output
    assert "catalog service unreachable" in result.output


def test_flip_to_the_same_status_marks_nothing(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 15, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2)],
        links=[_link("1.7.2", "1.7.1", "supersedes")],
    )
    t2 = _FakeT2Client({(_project(tmp_path), "15"): {"content": "status: accepted\n", "tags": ""}})
    _install(monkeypatch, tmp_path, cat, t2)

    assert _flip(tmp_path, 14, "accepted").exit_code == 0
    assert t2.puts == []


# ---------------------------------------------------------------------------
# rdr-audit lists every record carrying the marker
# ---------------------------------------------------------------------------


def test_audit_listing_reports_every_marker_and_nothing_else():
    project = "proj_rdr"
    t2 = _FakeT2Client({
        (project, "15"): {"content": "status: accepted\nneeds-reexamination: RDR-14 accepted->closed\n", "tags": ""},
        (project, "RDR-16"): {
            "content": (
                "status: accepted\nneeds-reexamination: RDR-14 accepted->closed\n"
                "needs-reexamination: RDR-15 accepted->abandoned\n"
            ),
            "tags": "",
        },
        (project, "17"): {"content": "status: closed\n", "tags": ""},
        (project, "015-gate-latest"): {"content": "outcome: passed\nneeds-reexamination: bogus\n", "tags": ""},
    })
    rows, error = rdr_mod._t2_needs_reexamination_markers("proj", client_factory=lambda: t2)
    assert error is None
    assert rows == [
        ("15", "needs-reexamination: RDR-14 accepted->closed"),
        ("RDR-16", "needs-reexamination: RDR-14 accepted->closed"),
        ("RDR-16", "needs-reexamination: RDR-15 accepted->abandoned"),
    ]


def test_audit_listing_reports_t2_failure_on_its_own_line():
    def _boom():
        raise ConnectionError("T2 down")

    rows, error = rdr_mod._t2_needs_reexamination_markers("proj", client_factory=_boom)
    assert rows == []
    assert error is not None and "T2 down" in error


def test_audit_preamble_prints_the_marker_section(monkeypatch, capsys):
    project = "nexus_rdr"
    t2 = _FakeT2Client({
        (project, "159"): {"content": "status: superseded\nneeds-reexamination: RDR-185 accepted->closed\n", "tags": ""},
    })
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: t2)
    monkeypatch.setenv("NEXUS_PROJECT_ROOTS", "/nonexistent-root-for-this-test")
    result = CliRunner().invoke(rdr, ["preamble", "rdr-audit", "nexus"])
    assert result.exit_code == 0, result.output
    assert "**Needs re-examination (T2 `nexus_rdr` markers):**" in result.output
    assert "- `159`: needs-reexamination: RDR-185 accepted->closed" in result.output


# ---------------------------------------------------------------------------
# .23 code review (T2 [24088]) I1-I3: attribution kept, unmappable neighbours
# named, one failing entry never hides the others
# ---------------------------------------------------------------------------


def test_marker_put_passes_the_entry_agent_and_session_back(tmp_path, monkeypatch):
    """The engine upserts and re-stamps agent/session from the payload; the
    facade fills them from the FLIPPING process's env. Passing the entry's
    own values back is the only way a marker does not re-attribute the
    record."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 15, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2)],
        links=[_link("1.7.1", "1.7.2", "supersedes")],
    )
    project = _project(tmp_path)

    class _AttributingT2(_FakeT2Client):
        def put(self, project, title, content, tags="", ttl=30, **kw):
            self.puts.append({"project": project, "title": title, "tags": tags, "ttl": ttl, **kw})
            return 1

    t2 = _AttributingT2({(project, "15"): {"content": "status: accepted\n", "tags": "rdr", "agent": "architect-planner", "session": "sess-1"}})
    _install(monkeypatch, tmp_path, cat, t2)
    assert _flip(tmp_path, 14, "closed").exit_code == 0
    assert t2.puts == [{"project": project, "title": "15", "tags": "rdr", "ttl": None, "agent": "architect-planner", "session": "sess-1"}]


def test_unmappable_supersedes_neighbour_is_named_not_dropped(tmp_path, monkeypatch, capsys):
    """A neighbour tumbler the numeric index cannot map back to an RDR
    number (RDR-040 / RDR-079 on the real tree: colliding registrations, no
    unique id: self-declaration) gets a note naming the tumbler, never a
    silent omission."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    orphan = _entry(tmp_path, 99, 9)
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1)],  # 1.7.9 is a supersedes target but never listed -> unmappable
        links=[_link("1.7.1", "1.7.9", "supersedes")],
    )
    del orphan
    t2 = _FakeT2Client({})
    _install(monkeypatch, tmp_path, cat, t2)
    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert t2.puts == []
    assert "supersedes neighbour 1.7.9 could not be mapped" in result.output


def test_one_failing_entry_does_not_hide_the_marks_already_written(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    for n in (14, 15, 16):
        _write_rdr(rdr_dir, n, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2), _entry(tmp_path, 16, 3)],
        links=[_link("1.7.1", "1.7.2", "supersedes"), _link("1.7.1", "1.7.3", "supersedes")],
    )
    project = _project(tmp_path)

    class _FlakyT2(_FakeT2Client):
        def put(self, project, title, content, tags="", ttl=30, **kw):
            if title == "16":
                raise ConnectionError("T2 hiccup on 16")
            return super().put(project, title, content, tags=tags, ttl=ttl, **kw)

    t2 = _FlakyT2({
        (project, "15"): {"content": "status: accepted\n", "tags": ""},
        (project, "16"): {"content": "status: accepted\n", "tags": ""},
    })
    _install(monkeypatch, tmp_path, cat, t2)
    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert [p["title"] for p in t2.puts] == ["15"]
    assert f"marked {project}/15 needs-reexamination" in result.output
    assert "dependents not marked: RDR 16: ConnectionError: T2 hiccup on 16" in result.output


def test_repeating_a_flip_does_not_stack_a_duplicate_marker(tmp_path, monkeypatch):
    """critique [24089] S3: markers accumulate on permanent entries; the same
    marker written twice (a status flipped there and back and there again,
    or a re-run) must not duplicate the line."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    _write_rdr(rdr_dir, 15, "accepted")
    cat = _FakeCatalog(
        entries=[_entry(tmp_path, 14, 1), _entry(tmp_path, 15, 2)],
        links=[_link("1.7.1", "1.7.2", "supersedes")],
    )
    project = _project(tmp_path)
    t2 = _FakeT2Client({(project, "15"): {"content": "status: accepted\n", "tags": ""}})
    _install(monkeypatch, tmp_path, cat, t2)
    assert _flip(tmp_path, 14, "closed").exit_code == 0
    assert len(t2.puts) == 1
    # simulate the file being flipped back by hand, then closed again
    _write_rdr(rdr_dir, 14, "accepted")
    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert len(t2.puts) == 1, "the identical marker must not be appended twice"
    assert f"marked {project}/15 needs-reexamination" in result.output


# ---------------------------------------------------------------------------
# rdr-audit's file-vs-T2 drift detector (critique [24089] Critical: the
# reconciler is gone, so the drift class needs a detector, not a writer)
# ---------------------------------------------------------------------------


def test_file_vs_t2_drift_reports_only_disagreements_on_both_surfaces():
    drift = rdr_mod._file_vs_t2_status_drift(
        {"14": "closed", "15": "accepted", "16": "open", "17": "draft", "18": "closed"},
        {"14": "superseded", "15": "accepted", "16": "draft", "18": "Closed", "99": "closed"},
    )
    assert drift == [("14", "closed", "superseded")]


def test_rdr_file_statuses_reads_this_shape_and_skips_companions(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "closed")
    (rdr_dir / "rdr-014-phase1-prereg.md").write_text("---\nkind: companion\nstatus: frozen\n---\n")
    (rdr_dir / "rdr137-legacy.md").write_text("---\nstatus: accepted\n---\n")
    (rdr_dir / "README.md").write_text("---\nstatus: draft\n---\n")
    assert rdr_mod._rdr_file_statuses(rdr_dir) == {"14": "closed", "137": "accepted"}


def test_audit_preamble_prints_drift_lines(monkeypatch, tmp_path, capsys):
    root = tmp_path / "nexus"
    rdr_dir = root / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write_rdr(rdr_dir, 159, "closed")
    _write_rdr(rdr_dir, 160, "accepted")
    project = "nexus_rdr"
    t2 = _FakeT2Client({
        (project, "159"): {"content": "status: superseded\n", "tags": ""},
        (project, "160"): {"content": "status: accepted\n", "tags": ""},
    })
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: t2)
    monkeypatch.setenv("NEXUS_PROJECT_ROOTS", str(tmp_path))
    result = CliRunner().invoke(rdr, ["preamble", "rdr-audit", "nexus"])
    assert result.exit_code == 0, result.output
    assert "- DRIFT: RDR-159 file=`closed` T2=`superseded`" in result.output
    assert "1 file-vs-T2 status disagreement(s)" in result.output


# ---------------------------------------------------------------------------
# set-status mirrors the flip onto the record's own T2 entry (Sam, 2026-09-02:
# until then the lifecycle skills were the only T2 status writer, in prose)
# ---------------------------------------------------------------------------


def test_set_status_rewrites_the_t2_status_line_and_date(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    project = _project(tmp_path)
    t2 = _FakeT2Client({(project, "14"): {"content": "title: x\nstatus: accepted\naccepted_date: 2026-01-01\n", "tags": "rdr"}})
    _install(monkeypatch, tmp_path, _FakeCatalog(entries=[_entry(tmp_path, 14, 1)]), t2)
    result = CliRunner().invoke(rdr, ["set-status", "14", "closed", "--date", "2026-09-02", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert t2.puts[0]["content"] == "title: x\nstatus: closed\nclosed_date: 2026-09-02\naccepted_date: 2026-01-01\n"
    assert t2.puts[0]["ttl"] is None and t2.puts[0]["tags"] == "rdr"
    assert f"updated T2 {project}/14 status -> closed" in result.output


def test_set_status_prepends_a_status_line_when_the_entry_has_only_prose(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 14, "accepted")
    project = _project(tmp_path)
    t2 = _FakeT2Client({(project, "RDR-14"): {"content": "RDR-14 thing. STATUS: accepted.\n", "tags": ""}})
    _install(monkeypatch, tmp_path, _FakeCatalog(entries=[_entry(tmp_path, 14, 1)]), t2)
    result = CliRunner().invoke(rdr, ["set-status", "14", "closed", "--date", "2026-09-02", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert t2.puts[0]["title"] == "RDR-14"
    assert t2.puts[0]["content"] == "status: closed\nclosed_date: 2026-09-02\nRDR-14 thing. STATUS: accepted.\n"


def test_set_status_names_a_missing_t2_entry_and_still_flips(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    path = _write_rdr(rdr_dir, 14, "accepted")
    _install(monkeypatch, tmp_path, _FakeCatalog(entries=[_entry(tmp_path, 14, 1)]), _FakeT2Client({}))
    result = _flip(tmp_path, 14, "closed")
    assert result.exit_code == 0, result.output
    assert "status: closed" in path.read_text()
    assert "no T2 entry for RDR 14" in result.output
