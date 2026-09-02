# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``nx rdr set-status`` — the code-enforced frontmatter flip.

Root-cause fix for the RDR accept/close *ledger-drift* class (RDR-165 / RDR-166,
2026-06-24): the accept step wrote T2 ``status: accepted`` but the RDR *file*
frontmatter was never flipped from ``draft`` because the flip was a soft,
agent-driven skill instruction that silently got skipped. ``rdr-close`` then
BLOCKED on the stale file status and required manual reconciliation.

This command makes the flip a single, deterministic, tested filesystem action
that the accept/close skills call instead of editing the frontmatter by hand.
It rewrites the RDR file ``status:`` line (plus the matching ``accepted_date``
/ ``closed_date`` key) and the README index-row status cell.

RDR-201 P1.4 (nexus-j9z30.4) rewires this command onto the packaged
``rdr-lifecycle`` state-machine table (``src/nexus/tables/rdr-lifecycle.toml``,
loaded via ``load_packaged_table`` so it is reachable from an installed
wheel). The requested status becomes the table's ``event`` dimension via a
small explicit (current, target) -> event mapping in ``rdr.py``; the file's
current frontmatter status binds the ``status`` dimension. An illegal edge
refuses with the table row's typed refuse code instead of silently
succeeding — this is a deliberate behavior change from the old
``_KNOWN_STATUSES`` membership check, which allowed ANY status word to flip
to ANY other (e.g. draft straight to closed).

GATE BINDING: the ``accept`` event's ``gate`` dimension is read from T2
(project ``<repo>_rdr``, title ``<id>-gate-latest``, the same coordinates
``nx rdr preamble rdr-accept`` already prints) via ``_gate_outcome_for``,
through the injectable ``_t2_client_factory`` seam — production code
constructs a real ``T2Database``; tests monkeypatch the factory to a fake
client so no test touches a live T2 substrate. A missing gate record and an
unreachable T2 both reduce to ``gate="none"``, which the table refuses as
``gate-not-passed`` — never a silent pass. No other event consults T2.

IDEMPOTENCY: re-requesting the record's CURRENT status is a no-op (exit 0,
file untouched) for every status, not only ``draft`` — the ``rdr-accept``
self-heal path and repeated ``rdr-close`` runs depend on this (Sam,
2026-09-02).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import nexus.commands.rdr as rdr_mod
from nexus.commands.rdr import (
    _from_statuses_for_event,
    _gate_repo_name,
    _rewrite_frontmatter_status,
    _to_status_for_event,
    rdr,
)
from nexus.tables.load import TableLoadError, load_packaged_table


def _runner() -> CliRunner:
    return CliRunner()


def _rdr_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    return d


_RDR_BODY = """## Problem Statement

Some prose.

## Decision

A decision.
"""


def _write_rdr(rdr_dir: Path, num: int, status: str, extra_fm: str = "") -> Path:
    fm = (
        "---\n"
        f'title: "RDR-{num:03d} Example Title"\n'
        f"id: RDR-{num:03d}\n"
        "type: Architecture\n"
        f"status: {status}\n"
        "priority: high\n"
        "created: 2026-06-22\n"
        f"{extra_fm}"
        "---\n\n"
    )
    p = rdr_dir / f"rdr-{num:03d}-example-title.md"
    p.write_text(fm + _RDR_BODY, encoding="utf-8")
    return p


def _write_readme(rdr_dir: Path, num: int, status_cell: str) -> Path:
    readme = rdr_dir / "README.md"
    readme.write_text(
        "# RDR Index\n\n"
        "| RDR | Title | Type | Status | Date |\n"
        "|-----|-------|------|--------|------|\n"
        f"| [RDR-{num:03d}](rdr-{num:03d}-example-title.md) | RDR-{num:03d} Example "
        f"Title | Architecture | {status_cell} | 2026-06-22 |\n",
        encoding="utf-8",
    )
    return readme


def _invoke(rdr_dir: Path, *args: str):
    return _runner().invoke(
        rdr, ["set-status", *args, "--root", str(rdr_dir.parent.parent)]
    )


def _gate_coords(tmp_path: Path, num: int) -> tuple[str, str]:
    """(project, title) `_gate_outcome_for` will look up for RDR *num* when
    the CLI is invoked with ``--root`` pointing at *tmp_path* — repo_name is
    derived from the root path's basename (``Path(repo_root).name``)."""
    return f"{tmp_path.name}_rdr", f"{num}-gate-latest"


def _gate_record(outcome: str) -> dict[str, Any]:
    return {"content": f"outcome: {outcome}\n"}


class _FakeT2Client:
    """Test double for the injectable ``_t2_client_factory`` seam.

    Satisfies the same minimal contract the real ``T2Database`` facade does:
    a context manager exposing ``get(project=..., title=...) -> dict | None``.
    Tracks ``get_call_count`` so a test can assert T2 was NEVER consulted
    (rather than asserting on a swallowed-exception side effect — code
    review, T2 nexus/code-review-nexus-j9z30-4-2026-09-01 [24033]
    finding 4).
    """

    def __init__(
        self,
        entries: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        raise_on_get: Exception | None = None,
    ) -> None:
        self._entries = entries or {}
        self._raise_on_get = raise_on_get
        self.get_call_count = 0

    def __enter__(self) -> "_FakeT2Client":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def get(
        self, project: str | None = None, title: str | None = None, id: int | None = None
    ) -> dict[str, Any] | None:
        self.get_call_count += 1
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._entries.get((project, title))


def _install_fake_t2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: dict[tuple[str, str], dict[str, Any]] | None = None,
    raise_on_get: Exception | None = None,
) -> _FakeT2Client:
    fake = _FakeT2Client(entries=entries, raise_on_get=raise_on_get)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Legal transitions (succeed)
# ---------------------------------------------------------------------------


def test_draft_to_accepted_with_gate_passed_succeeds(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 200, "draft")
    _write_readme(rdr_dir, 200, "Draft")
    project, title = _gate_coords(tmp_path, 200)
    _install_fake_t2(monkeypatch, entries={(project, title): _gate_record("PASSED")})

    res = _invoke(rdr_dir, "200", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "status: accepted" in text
    assert "status: draft" not in text
    assert "accepted_date: 2026-06-24" in text
    # body preserved
    assert "## Problem Statement" in text
    assert "## Decision" in text


def test_accepted_to_closed_flips_file_and_adds_closed_date(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 201, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    _write_readme(rdr_dir, 201, "Accepted")

    res = _invoke(rdr_dir, "201", "closed", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "status: closed" in text
    assert "closed_date: 2026-06-24" in text
    # accepted_date preserved, not duplicated
    assert text.count("accepted_date:") == 1
    assert "accepted_date: 2026-06-22" in text


def test_present_but_blank_closed_date_is_filled(tmp_path):
    """nexus-re3nm: same for a blank ``closed_date:`` on a close flip."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(
        rdr_dir, 211, "accepted",
        extra_fm="accepted_date: 2026-06-22\nclosed_date:\n",
    )
    _write_readme(rdr_dir, 211, "Accepted")

    res = _invoke(rdr_dir, "211", "closed", "--date", "2026-06-25")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "closed_date: 2026-06-25" in text
    assert text.count("closed_date:") == 1
    # accepted_date untouched
    assert "accepted_date: 2026-06-22" in text


def test_readme_status_cell_updated(tmp_path):
    """A legal transition (accepted -> closed) rewrites the README cell."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 202, "accepted")
    readme = _write_readme(rdr_dir, 202, "Accepted")

    res = _invoke(rdr_dir, "202", "closed", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    row = [ln for ln in readme.read_text().splitlines() if "RDR-202" in ln][0]
    assert "| Closed |" in row
    assert "Accepted" not in row


def test_deferred_to_draft_succeeds_resume(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 220, "deferred")
    _write_readme(rdr_dir, 220, "Deferred")

    res = _invoke(rdr_dir, "220", "draft", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: draft" in text
    assert "status: deferred" not in text


def test_accepted_to_deferred_succeeds(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 221, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    _write_readme(rdr_dir, 221, "Accepted")

    res = _invoke(rdr_dir, "221", "deferred", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: deferred" in text


def test_supersede_with_successor_named_succeeds(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 222, "accepted", extra_fm="superseded_by: RDR-999\n")
    readme = _write_readme(rdr_dir, 222, "Accepted")

    res = _invoke(rdr_dir, "222", "superseded", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: superseded" in text

    # The README cell is decorated with the successor id — the frontmatter
    # `superseded_by` key is on the OLD file, not the index, so a bare
    # "Superseded" cell would be the only place that link is lost (code
    # review, T2 nexus/critique-nexus-j9z30-4-2026-09-01 [24034] finding 9).
    row = [ln for ln in readme.read_text().splitlines() if "RDR-222" in ln][0]
    assert "Superseded by RDR-999" in row


def test_draft_to_abandoned_succeeds(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 233, "draft")
    _write_readme(rdr_dir, 233, "Draft")

    res = _invoke(rdr_dir, "233", "abandoned", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    assert "status: abandoned" in f.read_text()


def test_accepted_to_abandoned_succeeds(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 234, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    _write_readme(rdr_dir, 234, "Accepted")

    res = _invoke(rdr_dir, "234", "abandoned", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    assert "status: abandoned" in f.read_text()


def test_deferred_to_abandoned_succeeds(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 235, "deferred")
    _write_readme(rdr_dir, 235, "Deferred")

    res = _invoke(rdr_dir, "235", "abandoned", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    assert "status: abandoned" in f.read_text()


def test_open_to_accepted_with_gate_passed_succeeds(tmp_path, monkeypatch):
    """`open` is a retired status word still advertised by the rdr-accept
    preamble as a live pre-accept synonym for `draft` (nexus-qsryj). It
    must resolve as draft (including consulting the gate) without ever
    being written back to the file."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 236, "open")
    _write_readme(rdr_dir, 236, "Draft")
    project, title = _gate_coords(tmp_path, 236)
    _install_fake_t2(monkeypatch, entries={(project, title): _gate_record("PASSED")})

    res = _invoke(rdr_dir, "236", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: accepted" in text
    assert "status: open" not in text
    assert "alias" in res.output.lower() or "open" in res.output.lower()


def test_open_to_closed_refuses_illegal_transition(tmp_path):
    """open == draft for resolution purposes; draft -> closed is illegal
    (only accepted -> closed is a legal `close` edge)."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 237, "open")
    before = f.read_text()

    res = _invoke(rdr_dir, "237", "closed", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "illegal-transition" in res.output
    assert f.read_text() == before  # untouched, including status: open preserved


def test_draft_to_draft_is_noop(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 223, "draft")
    before = f.read_text()

    res = _invoke(rdr_dir, "223", "draft")
    assert res.exit_code == 0, res.output
    assert f.read_text() == before  # untouched


def test_accepted_to_accepted_is_noop(tmp_path):
    """Same-status re-run is a no-op for EVERY status, not only draft — the
    rdr-accept self-heal path and repeated rdr-close runs depend on
    set-status being idempotent (Sam, 2026-09-02). No T2 gate read happens
    here: the no-op short-circuit fires before the event is even computed."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 204, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    before = f.read_text()

    res = _invoke(rdr_dir, "204", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    assert "no-op" in res.output.lower()
    assert f.read_text() == before  # untouched


def test_command_works_with_no_docs_tables_dir_at_all(tmp_path):
    """The lifecycle table is PACKAGED (src/nexus/tables/), never read from a
    repo-relative docs/tables/ path — the command must work identically in a
    repo that has no docs/tables/ directory whatsoever (the wheel-install
    case; RDR-201 P1.3's TABLE LOCATION note)."""
    rdr_dir = _rdr_dir(tmp_path)
    assert not (tmp_path / "docs" / "tables").exists()
    f = _write_rdr(rdr_dir, 224, "accepted")
    _write_readme(rdr_dir, 224, "Accepted")

    res = _invoke(rdr_dir, "224", "closed", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    assert "status: closed" in f.read_text()
    assert not (tmp_path / "docs" / "tables").exists()


def test_body_with_horizontal_rule_is_preserved(tmp_path):
    """A '---' inside the body must not be mistaken for the frontmatter fence."""
    rdr_dir = _rdr_dir(tmp_path)
    f = rdr_dir / "rdr-205-example-title.md"
    f.write_text(
        "---\n"
        'title: "RDR-205 Example Title"\n'
        "id: RDR-205\n"
        "type: Architecture\n"
        "status: draft\n"
        "priority: high\n"
        "created: 2026-06-22\n"
        "---\n\n"
        "## Section A\n\nText.\n\n---\n\n## Section B\n\nMore text.\n",
        encoding="utf-8",
    )
    _write_readme(rdr_dir, 205, "Draft")

    # draft -> deferred: unconditional (no gate guard), unlike accept.
    res = _invoke(rdr_dir, "205", "deferred", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "status: deferred" in text
    assert "## Section A" in text
    assert "## Section B" in text
    # the body horizontal rule survives
    assert "\n---\n\n## Section B" in text


# ---------------------------------------------------------------------------
# Illegal transitions (refuse, typed reason)
# ---------------------------------------------------------------------------


def test_draft_to_closed_refuses_illegal_transition(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 203, "draft")
    before = f.read_text()

    res = _invoke(rdr_dir, "203", "closed", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "illegal-transition" in res.output
    assert f.read_text() == before  # untouched


def test_closed_to_abandoned_refuses_illegal_transition(tmp_path):
    """closed is terminal — abandon is only legal from draft/accepted/deferred."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 238, "closed", extra_fm="accepted_date: 2026-06-20\nclosed_date: 2026-06-22\n")
    before = f.read_text()

    res = _invoke(rdr_dir, "238", "abandoned", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "illegal-transition" in res.output
    assert f.read_text() == before  # untouched


def test_deferred_to_accepted_refuses(tmp_path, monkeypatch):
    """The ruling's sharpest edge: deferred resumes to draft only, never
    directly to accepted. gate is only ever consulted for event=='accept'
    AND current_status=='draft' — deferred is not draft, so this must
    refuse WITHOUT touching T2 at all. Asserted on the fake's call count,
    not on an exception surfacing (code review, T2
    nexus/code-review-nexus-j9z30-4-2026-09-01 [24033] finding 4: a prior
    version of this test used ``raise_on_get`` as a sentinel, but
    ``_gate_outcome_for``'s broad ``except Exception`` silently swallowed
    it into a misleading gate_note, so the test passed whether or not T2
    was actually consulted)."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 225, "deferred")
    before = f.read_text()
    fake = _install_fake_t2(monkeypatch, entries={})

    res = _invoke(rdr_dir, "225", "accepted", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "illegal-transition" in res.output
    assert "T2 unreachable" not in res.output
    assert fake.get_call_count == 0
    assert f.read_text() == before  # untouched


def test_supersede_without_successor_refuses_successor_not_named(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 226, "accepted")  # no superseded_by
    before = f.read_text()

    res = _invoke(rdr_dir, "226", "superseded", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "successor-not-named" in res.output
    assert f.read_text() == before  # untouched


def test_draft_to_accepted_with_gate_blocked_refuses(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 230, "draft")
    before = f.read_text()
    project, title = _gate_coords(tmp_path, 230)
    _install_fake_t2(monkeypatch, entries={(project, title): _gate_record("BLOCKED")})

    res = _invoke(rdr_dir, "230", "accepted", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "gate-not-passed" in res.output
    assert f.read_text() == before  # untouched


def test_draft_to_accepted_with_no_gate_record_refuses_and_names_it(tmp_path, monkeypatch):
    """No T2 gate record at all -> gate-not-passed, and the message names
    the missing record rather than a bare refusal."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 231, "draft")
    before = f.read_text()
    _install_fake_t2(monkeypatch, entries={})  # no matching record

    res = _invoke(rdr_dir, "231", "accepted", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "gate-not-passed" in res.output
    assert "no gate record found" in res.output
    assert "231-gate-latest" in res.output
    assert f.read_text() == before  # untouched


def test_draft_to_accepted_t2_unreachable_refuses_and_says_so(tmp_path, monkeypatch):
    """T2 itself cannot be reached (e.g. a ConnectionError from the client)
    -> gate-not-passed, message names T2 as unreachable rather than
    crashing the CLI or silently passing the gate."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 232, "draft")
    before = f.read_text()
    _install_fake_t2(monkeypatch, raise_on_get=ConnectionError("connection refused"))

    res = _invoke(rdr_dir, "232", "accepted", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "gate-not-passed" in res.output
    assert "T2 unreachable" in res.output
    assert f.read_text() == before  # untouched


def test_gate_repo_name_resolves_worktree_to_main_repo_name(tmp_path: Path) -> None:
    """The T2 gate project must key off the MAIN checkout's basename, not
    the WORKTREE directory's own basename — plain ``Path(repo_root).name``
    would resolve a Claude Code agent worktree (e.g.
    ``agent-a9b6e48835b938551``) to a per-agent T2 project no gate result
    was ever written to (code review, T2
    nexus/critique-nexus-j9z30-4-2026-09-01 [24034] finding 8). Same git
    idiom as ``tests/test_repo_identity_stability.py``'s worktree test."""
    main = tmp_path / "mainrepo"
    main.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=main, check=True, capture_output=True)
    (main / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "seed.txt"], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "seed", "--quiet"],
        cwd=main, check=True, capture_output=True,
    )
    worktree = tmp_path / "worktrees" / "agent-a9b6e48835b938551"
    worktree.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "--quiet", str(worktree), "-b", "feature"],
        cwd=main, check=True, capture_output=True,
    )

    assert _gate_repo_name(str(main)) == "mainrepo"
    assert _gate_repo_name(str(worktree)) == "mainrepo"


def test_gate_repo_name_falls_back_to_basename_when_not_a_git_repo(tmp_path: Path) -> None:
    """A *repo_root* that is not a git repo at all (e.g. a bare tmp_path in
    every other test in this module) falls back to its own basename
    unchanged — matching every existing test's ``_gate_coords`` assumption
    (``tmp_path.name``)."""
    not_a_repo = tmp_path / "not-a-git-repo"
    not_a_repo.mkdir()
    assert _gate_repo_name(str(not_a_repo)) == "not-a-git-repo"


# ---------------------------------------------------------------------------
# Caller/defect errors (unknown ID, unknown status, unparsable table)
# ---------------------------------------------------------------------------


def test_unknown_id_errors_nonzero(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 203, "draft")
    res = _invoke(rdr_dir, "999", "closed", "--date", "2026-06-24")
    assert res.exit_code != 0


def test_unknown_status_errors_and_does_not_write(tmp_path):
    """A typo'd status must be rejected, not silently written to the file."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 206, "draft")
    before = f.read_text()
    res = _invoke(rdr_dir, "206", "clsoed", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "unknown status" in res.output.lower()
    assert f.read_text() == before  # untouched


def test_unparsable_table_exits_2_no_fallback(tmp_path, monkeypatch):
    """Table missing or unparsable: exit 2, NO silent fallback to a
    hardcoded status list (RDR-201 § Failure Modes)."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 227, "draft")
    before = f.read_text()

    def _boom(resource, package="nexus.tables"):
        raise TableLoadError("planted failure for the test")

    monkeypatch.setattr(rdr_mod, "load_packaged_table", _boom)

    res = _invoke(rdr_dir, "227", "closed", "--date", "2026-06-24")
    assert res.exit_code == 2, res.output
    assert f.read_text() == before  # untouched


# ---------------------------------------------------------------------------
# README index-cell detection (decorated cells, membership set from the
# table's status domain rather than the deleted _KNOWN_STATUSES literal)
# ---------------------------------------------------------------------------


def test_readme_decorated_cell_leading_word_detected(tmp_path):
    """A decorated README cell (e.g. 'Accepted (foo)') is still detected by
    its leading word, case-insensitive — the table's status domain is a
    membership set on the leading word, not an exact-cell match."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 228, "accepted")
    readme = _write_readme(rdr_dir, 228, "Accepted (foo)")

    res = _invoke(rdr_dir, "228", "closed", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    row = [ln for ln in readme.read_text().splitlines() if "RDR-228" in ln][0]
    assert "| Closed |" in row


# ---------------------------------------------------------------------------
# _rewrite_frontmatter_status direct unit coverage (date-key insertion/fill
# logic, independent of the CLI's transition-legality plumbing above).
# ---------------------------------------------------------------------------


def test_rewrite_frontmatter_status_adds_accepted_date():
    text = (
        "---\n"
        'title: "RDR-300 Example"\n'
        "status: draft\n"
        "---\n\n## Body\n"
    )
    new_text = _rewrite_frontmatter_status(text, "accepted", "2026-06-24")
    assert "status: accepted" in new_text
    assert "accepted_date: 2026-06-24" in new_text
    assert "## Body" in new_text


def test_rewrite_frontmatter_status_fills_blank_accepted_date():
    """nexus-re3nm: the RDR template ships ``accepted_date:`` blank. A flip
    to accepted must FILL it, not skip because the key is present."""
    text = (
        "---\n"
        'title: "RDR-301 Example"\n'
        "status: draft\n"
        "accepted_date:\n"
        "---\n\n## Body\n"
    )
    new_text = _rewrite_frontmatter_status(text, "accepted", "2026-06-25")
    assert "accepted_date: 2026-06-25" in new_text
    assert new_text.count("accepted_date:") == 1
    assert "accepted_date:\n" not in new_text


def test_rewrite_frontmatter_status_idempotent_does_not_overwrite_date():
    text = (
        "---\n"
        'title: "RDR-302 Example"\n'
        "status: accepted\n"
        "accepted_date: 2026-06-22\n"
        "---\n\n## Body\n"
    )
    new_text = _rewrite_frontmatter_status(text, "accepted", "2026-06-24")
    assert "accepted_date: 2026-06-22" in new_text
    assert "2026-06-24" not in new_text
    assert new_text.count("status:") == 1
    assert new_text.count("accepted_date:") == 1



# ---------------------------------------------------------------------------
# RDR-201 P1.5: table-derived status-list helpers used by the accept/close
# preambles (nexus-j9z30.5). These query the loaded table's rows directly —
# not a hand-maintained literal — so a table change is reflected automatically.
# ---------------------------------------------------------------------------


def test_from_statuses_for_event_returns_accept_source():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert _from_statuses_for_event(table, "accept") == frozenset({"draft"})


def test_from_statuses_for_event_returns_close_source():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert _from_statuses_for_event(table, "close") == frozenset({"accepted"})


def test_from_statuses_for_event_returns_multiple_sources_for_supersede():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert _from_statuses_for_event(table, "supersede") == frozenset(
        {"draft", "accepted", "deferred", "closed"}
    )


def test_from_statuses_for_event_unknown_event_is_empty():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert _from_statuses_for_event(table, "no-such-event") == frozenset()


def test_to_status_for_event_returns_accept_target():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert _to_status_for_event(table, "accept") == "accepted"


def test_to_status_for_event_returns_close_target():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert _to_status_for_event(table, "close") == "closed"


def test_to_status_for_event_refuses_when_no_single_target(tmp_path: Path):
    """supersede has ONE target (superseded) but FOUR sources -- to_status is
    still well-defined there. Construct a table where an event's non-escape
    rows genuinely disagree on target to prove the ambiguity refusal fires,
    rather than asserting on a table that happens not to exercise it."""
    from nexus.tables.load import load_table

    bad = tmp_path / "bad-lifecycle.toml"
    bad.write_text(
        """
[table]
id = "bad"
kind = "state-machine"

[dimensions.status]
domain = ["a", "b", "c"]
[dimensions.event]
domain = ["mix"]

[[row]]
id = "mix-a"
match = { status = "a", event = "mix" }
to = { status = "b" }

[[row]]
id = "mix-c"
match = { status = "c", event = "mix" }
to = { status = "a" }
"""
    )
    table = load_table(bad)
    with pytest.raises(TableLoadError):
        _to_status_for_event(table, "mix")


# ---------------------------------------------------------------------------
# RDR-201 P1.5: the rdr-accept / rdr-close preambles' eligible-status
# guards derive from these helpers against the LOADED table, not an
# independently hand-typed literal (nexus-j9z30.5). A table swapped in via
# monkeypatch with a DIFFERENT accept/close source status proves the
# binding: a test that only exercised the real table would pass even
# against a hardcoded ("draft", "open") / ("accepted", "final") literal
# that happened to still agree with it.
#
# No ``rdr_env``/T2Database fixture here (that module's fixture needs a
# live T2 service substrate, out of this bead's instructed test scope) --
# ``_preamble_resolve_repo()`` falls back to ``Path.cwd()`` when ``git
# rev-parse`` fails, so a bare ``monkeypatch.chdir(tmp_path)`` is enough.
# ---------------------------------------------------------------------------


def _write_fake_lifecycle_table(tmp_path: Path):
    """A minimal, loadable rdr-lifecycle-shaped table whose accept/close
    source statuses are NOT ``draft``/``accepted``."""
    from nexus.tables.load import load_table

    path = tmp_path / "fake-lifecycle.toml"
    path.write_text(
        """
[table]
id = "fake-lifecycle"
kind = "state-machine"

[dimensions.status]
domain = ["backlog", "greenlit", "shipped"]
[dimensions.event]
domain = ["accept", "close"]
[dimensions.gate]
domain = ["passed", "blocked", "none"]
[dimensions.successor]
domain = ["named", "absent"]

[[row]]
id = "accept"
match = { status = "backlog", event = "accept" }
guard = { gate = "passed" }
to = { status = "greenlit" }

[[row]]
id = "accept-blocked"
match = { status = "backlog", event = "accept" }
guard = { gate = ["blocked", "none"] }
refuse = "gate-not-passed"

[[row]]
id = "accept-otherwise"
match = { status = ["greenlit", "shipped"], event = "accept" }
escape = true
refuse = "illegal-transition"

[[row]]
id = "close"
match = { status = "greenlit", event = "close" }
to = { status = "shipped" }

[[row]]
id = "close-otherwise"
match = { status = ["backlog", "shipped"], event = "close" }
escape = true
refuse = "illegal-transition"
"""
    )
    return load_table(path)


def _preamble_rdr_dir_for(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_preamble_rdr(rdr_dir: Path, num: int, status: str, title: str) -> Path:
    p = rdr_dir / f"rdr-{num:03d}-example.md"
    p.write_text(
        "---\n"
        f'title: "{title}"\n'
        "type: Architecture\n"
        f"status: {status}\n"
        "priority: high\n"
        "---\n\n## Problem Statement\n\nProblem.\n",
        encoding="utf-8",
    )
    return p


class TestAcceptCloseGuardsDeriveFromTable:
    """Proves the accept/close preamble guards and listings are bound to
    the loaded table's rows, not an independently hand-typed literal."""

    def test_accept_listing_follows_table_accept_source(self, tmp_path, monkeypatch):
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "backlog", "Backlog One")

        result = _runner().invoke(rdr, ["preamble", "rdr-accept"])
        assert result.exit_code == 0, result.output
        assert "Backlog One" in result.output

    def test_accept_listing_excludes_real_table_draft_when_table_differs(
        self, tmp_path, monkeypatch
    ):
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "draft", "Still Draft")

        result = _runner().invoke(rdr, ["preamble", "rdr-accept"])
        assert result.exit_code == 0, result.output
        assert "Still Draft" not in result.output

    def test_accept_guard_follows_table_accept_source(self, tmp_path, monkeypatch):
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "backlog", "Backlog")

        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" not in result.output

    def test_accept_guard_blocks_real_table_draft_when_table_differs(
        self, tmp_path, monkeypatch
    ):
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "draft", "Draft")

        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output

    def test_accept_guard_allows_table_accept_target_idempotently(
        self, tmp_path, monkeypatch
    ):
        """The already-accepted allowance follows the table's accept TARGET
        (``greenlit`` here), not the real table's ``accepted``."""
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "greenlit", "Greenlit")

        result = _runner().invoke(rdr, ["preamble", "rdr-accept", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" not in result.output

    def test_close_guard_follows_table_close_source(self, tmp_path, monkeypatch):
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "greenlit", "Greenlit")

        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" not in result.output

    def test_close_guard_blocks_real_table_accepted_when_table_differs(
        self, tmp_path, monkeypatch
    ):
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        fake_table = _write_fake_lifecycle_table(tmp_path)
        monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "accepted", "Accepted")

        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output

    def test_close_message_no_longer_names_retired_final_status(
        self, tmp_path, monkeypatch
    ):
        """``final`` is retired from the table's domain (RDR-201 Revision
        History); the close-preamble BLOCKED message must not advertise it
        as an acceptable close-source status any more."""
        rdr_dir = _preamble_rdr_dir_for(tmp_path)
        monkeypatch.chdir(tmp_path)
        _write_preamble_rdr(rdr_dir, 1, "draft", "Hello World")

        result = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "1"])
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "final" not in result.output.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
