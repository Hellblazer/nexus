# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared seeds and runners of the ``nx memory gate-rollup`` tests (``test_memory_gate_rollup.py`` and
``test_memory_gate_ttl.py``): gate records written as ``gate_record.lua`` writes them, a closed RDR-209 in the
working directory, and the command run through its CLI against the engine test substrate."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import make_t3
from nexus.db.t2 import T2Database
from nexus.memory_curate import gate_rollup as gr
from tests._t2_fixture_ops import backdate_memory

REPO = "gaterepo"
RDR = 209
POINTER_P3 = "gate-records-rdr-209-phase-3"


def _write_rdr(root: Path, number: int, status: str, phases: dict[str, str] | None = None) -> None:
    """An RDR file under ``root/docs/rdr``: the frontmatter status, and one heading per phase (``{"3": "CLOSED"}``)."""
    d = root / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(f"\n### Phase {n}{f' ({mark}, 2026-11-06)' if mark else ''}: work\n" for n, mark in (phases or {}).items())
    (d / f"rdr-{number:03d}-test.md").write_text(f"---\ntitle: t\nid: RDR-{number:03d}\nstatus: {status}\n---\n{body}")


@pytest.fixture(autouse=True)
def _closed_rdr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """RDR-209 is closed in the repository the command runs in (its working directory), unless a test says otherwise."""
    _write_rdr(tmp_path, RDR, "closed")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _put(db: T2Database, title: str, content: str = "body", tags: str = "", project: str = REPO,
         ttl: int | None = None, age_days: float | None = None) -> None:
    db.put(project=project, title=title, content=content, tags=tags, ttl=ttl)
    if age_days is not None:
        backdate_memory(db, project, title, days=age_days)


def _gate(db: T2Database, bead: str, *, kind: str = "review", sha: str = "abc1234", outcome: str = "justified",
          rdr: int | None = RDR, date: str = "2026-11-10", body: str = "Finding text.", rnd: int | None = None,
          tags: str | None = None, project: str = REPO, age_days: float | None = None,
          ttl: int | None = None, title: str | None = None) -> str:
    """Seed a gate record the way ``gate_record.lua`` writes it: header fields, a blank line, the body."""
    fields = f"kind: {kind}\nbead: {bead}\n"
    default_tags = f"kind:gate,bead:{bead}"
    if rdr is not None:
        fields += f"rdr: {rdr:03d}\n"
        default_tags += f",rdr:{rdr:03d}"
    fields += f"sha: {sha}\noutcome: {outcome}\ndate: {date}\n"
    name = title or f"{kind}-{sha}-{bead}-{date}" + (f"-r{rnd}" if rnd else "")
    _put(db, name, fields + "\n" + body, tags=default_tags if tags is None else tags, project=project,
         age_days=age_days, ttl=ttl)
    return name


def _rows(db: T2Database, project: str = REPO, *, with_counts: bool = False) -> dict[str, dict]:
    """The project's live rows by title, without the dry run's count rows (``gate-rollup-count-...``) unless asked."""
    return {r["title"]: r for r in db.get_all(project) if with_counts or not r["title"].startswith("gate-rollup-count-")}


def _run_raw(*args: str, expect: int = 0) -> str:
    """The command exactly as given: no dry run before it, no --counted added."""
    result = CliRunner().invoke(main, ["memory", "gate-rollup", "--rdr", str(RDR), "--project", REPO, *args])
    assert result.exit_code == expect, result.output
    return result.output


def _run(*args: str, expect: int = 0) -> str:
    """The command the way a person runs it: a dry run first (which records its count), then the real run with
    ``--counted <that count>``. A dry run that refuses ends the call there, with the refusal as the output. A call that
    is itself a dry run is made as given."""
    if "--dry-run" not in args:
        dry = CliRunner().invoke(main, ["memory", "gate-rollup", "--rdr", str(RDR), "--project", REPO, *args, "--dry-run"])
        if dry.exit_code != 0:
            assert expect != 0, dry.output
            return dry.output
        if m := re.search(r"quarantined by the next expire sweep: (\d+) of", dry.output):
            args = (*args, "--counted", m.group(1))
    return _run_raw(*args, expect=expect)


def _phase3(beads: str, *extra: str, expect: int = 0) -> str:
    return _run("--phase", "3", "--beads", beads, *extra, expect=expect)


def _pointer(title: str = POINTER_P3) -> gr.Pointer:
    got = gr.read_pointer(make_t3(), title)
    assert got is not None, f"no pointer record titled {title!r}"
    return got


def _titles(records: list[dict]) -> list[str]:
    return sorted(r["title"] for r in records)
