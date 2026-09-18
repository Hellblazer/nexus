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


def test_supersede_with_successor_named_succeeds(tmp_path, monkeypatch):
    """A ``superseded`` flip is the one transition that additionally calls
    ``_ensure_supersedes_edge``, which can WRITE a catalog link
    (``cat.link_if_absent``) -- unlike every other transition in this file,
    which only ever GETs. nexus-r8643 (intrastate review [26115] #3's
    sibling finding on this test): with no fakes installed here, that write
    path ran against the real ``_catalog_reader_factory`` /
    ``_t2_client_factory`` production seams. On a random ``tmp_path`` repo
    root it always fell through to the "no catalog owner registered"
    no-write branch in practice (nothing before this test ever registers
    an owner for that path), but the test itself gave no guarantee of
    that -- it asserted only on file text and relied on incidental
    non-collision. Fakes here make the "no live store" property
    unconditional rather than incidental; behavior asserted is unchanged
    (file text + README row only -- the catalog/T2 side effects this
    covers are pinned for real in
    tests/test_rdr_needs_reexamination.py's substrate-backed test)."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 222, "accepted", extra_fm="superseded_by: RDR-999\n")
    readme = _write_readme(rdr_dir, 222, "Accepted")
    _install_fake_t2(monkeypatch, entries={})
    monkeypatch.setattr(rdr_mod, "_catalog_reader_factory", lambda: object())
    monkeypatch.setattr(rdr_mod, "_rdr_repo_scope", lambda _cat, root: (None, ""))

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


def test_open_to_closed_without_reason_refuses(tmp_path):
    """open == draft for resolution purposes; draft -> closed is the guarded
    `close-unaccepted` edge (nexus-nc08w.4) and refuses without --reason."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 237, "open")
    before = f.read_text()

    res = _invoke(rdr_dir, "237", "closed", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "reason-not-stated" in res.output
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


def test_draft_to_closed_without_reason_refuses(tmp_path):
    """The stale-draft close edge (RDR-122, RDR-179 were hand-edited closed
    because none existed; nexus-nc08w.4) is guarded on a stated reason."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 203, "draft")
    before = f.read_text()

    res = _invoke(rdr_dir, "203", "closed", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "reason-not-stated" in res.output
    assert "--reason" in res.output
    assert f.read_text() == before  # untouched


def test_draft_to_closed_with_reason_succeeds(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 203, "draft")
    readme = _write_readme(rdr_dir, 203, "Draft")

    res = _invoke(rdr_dir, "203", "closed", "--date", "2026-06-24",
                  "--reason", "shipped under nexus-xyz without a gate")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: closed" in text
    assert "closed_date: 2026-06-24" in text
    row = [ln for ln in readme.read_text().splitlines() if "RDR-203" in ln][0]
    assert "| Closed |" in row


def test_reason_does_not_license_other_illegal_edges(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 205, "deferred")
    before = f.read_text()
    res = _invoke(rdr_dir, "205", "closed", "--reason", "no")
    assert res.exit_code != 0
    assert "illegal-transition" in res.output
    assert f.read_text() == before


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


def test_rewrite_frontmatter_status_stamps_its_own_date_key_once():
    """A flip's own date key takes the flip's date, replacing an earlier
    value (nexus-u1jxt.10, Sam 2026-09-18: a re-accept after a resume is a
    new acceptance). The key is written exactly once. The command never
    re-runs the rewriter on an RDR already in the target status (that path
    is the no-op branch), so this is the re-accept case, not idempotence."""
    text = (
        "---\n"
        'title: "RDR-302 Example"\n'
        "status: accepted\n"
        "accepted_date: 2026-06-22\n"
        "---\n\n## Body\n"
    )
    new_text = _rewrite_frontmatter_status(text, "accepted", "2026-06-24")
    assert "accepted_date: 2026-06-24" in new_text
    assert "2026-06-22" not in new_text
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


# ---------------------------------------------------------------------------
# RDR-201 P1.5 fix round (T2 nexus/critique-nexus-j9z30-5-2026-09-01 [24042]
# finding 4): _target_status_to_event derives the old hand-maintained
# _TARGET_STATUS_TO_EVENT literal from the table instead of carrying it
# forward as a survivor.
# ---------------------------------------------------------------------------


def test_target_status_to_event_matches_real_table():
    table = load_packaged_table("rdr-lifecycle.toml")
    assert rdr_mod._target_status_to_event(table) == {
        "accepted": "accept",
        "closed": "close",
        "superseded": "supersede",
        "abandoned": "abandon",
        "deferred": "defer",
    }


def test_target_status_to_event_excludes_resume():
    """``resume``'s target is ``draft``, ambiguous on its own -- it must
    never appear in the derived mapping (set_status resolves it separately
    from (current, target))."""
    table = load_packaged_table("rdr-lifecycle.toml")
    mapping = rdr_mod._target_status_to_event(table)
    assert "draft" not in mapping
    assert "resume" not in mapping.values()


def test_set_status_event_resolution_follows_table_not_hardcoded_literal(
    tmp_path, monkeypatch
):
    """CLI-level derivation proof: a fake table whose `close` event targets
    a status NAMED DIFFERENTLY from the real table's `closed` must still
    resolve and succeed through `_target_status_to_event`. Against the old
    hardcoded `_TARGET_STATUS_TO_EVENT` dict (whose keys are the real
    table's target names) this would KeyError instead of succeeding."""
    from nexus.tables.load import load_table

    fake_path = tmp_path / "fake-target-lifecycle.toml"
    fake_path.write_text(
        """
[table]
id = "fake-target-lifecycle"
kind = "state-machine"

[dimensions.status]
domain = ["draft", "accepted", "wrapped"]
[dimensions.event]
domain = ["accept", "close"]
[dimensions.gate]
domain = ["passed", "blocked", "none"]
[dimensions.successor]
domain = ["named", "absent"]

[[row]]
id = "accept"
match = { status = "draft", event = "accept" }
guard = { gate = "passed" }
to = { status = "accepted" }

[[row]]
id = "accept-blocked"
match = { status = "draft", event = "accept" }
guard = { gate = ["blocked", "none"] }
refuse = "gate-not-passed"

[[row]]
id = "accept-otherwise"
match = { status = ["accepted", "wrapped"], event = "accept" }
escape = true
refuse = "illegal-transition"

[[row]]
id = "close"
match = { status = "accepted", event = "close" }
to = { status = "wrapped" }

[[row]]
id = "close-otherwise"
match = { status = ["draft", "wrapped"], event = "close" }
escape = true
refuse = "illegal-transition"
"""
    )
    fake_table = load_table(fake_path)
    monkeypatch.setattr(rdr_mod, "load_packaged_table", lambda *a, **k: fake_table)

    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 900, "accepted")

    res = _invoke(rdr_dir, "900", "wrapped")

    assert res.exit_code == 0, res.output
    text = (rdr_dir / "rdr-900-example-title.md").read_text()
    assert "status: wrapped" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Intrastate review [26115] #2, #10, #12 (nexus-nc08w.3, nexus-nc08w.6)
# ---------------------------------------------------------------------------


class _FakeT2ReadWriteClient(_FakeT2Client):
    """``_FakeT2Client`` plus ``put``; ``fail_put`` simulates T2 down."""

    def __init__(self, entries, *, fail_put: bool = False) -> None:
        super().__init__(entries=entries)
        self.fail_put = fail_put

    def put(self, project, title, content, tags="", ttl=None, **kw):
        if self.fail_put:
            raise ConnectionError("T2 down")
        self._entries[(project, title)] = {"title": title, "content": content, "tags": tags}


def test_rerun_completes_the_t2_mirror_the_first_run_could_not(tmp_path, monkeypatch):
    """P1: with T2 unreachable the first run flips the file and exits 0 with
    a note; the re-run used to say 'already deferred (no-op)' before any T2
    work, so the record could never be mirrored."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 1, "draft")
    project = f"{tmp_path.name}_rdr"
    entries = {(project, "1"): {"title": "1", "content": "status: draft\ntitle: Foo\n"}}
    fake = _FakeT2ReadWriteClient(entries, fail_put=True)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    res = _invoke(rdr_dir, "1", "deferred")
    assert res.exit_code == 0, res.output
    assert "T2 status not mirrored" in res.output
    assert entries[(project, "1")]["content"].startswith("status: draft")

    fake.fail_put = False
    res = _invoke(rdr_dir, "1", "deferred")
    assert res.exit_code == 0, res.output
    assert entries[(project, "1")]["content"].startswith("status: deferred"), res.output
    assert "no-op" not in res.output.lower()


def test_rerun_with_t2_already_mirrored_is_still_a_noop(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 2, "deferred")
    project = f"{tmp_path.name}_rdr"
    entries = {(project, "2"): {"title": "2", "content": "status: deferred\n"}}
    fake = _FakeT2ReadWriteClient(entries)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    res = _invoke(rdr_dir, "2", "deferred")
    assert res.exit_code == 0, res.output
    assert "no-op" in res.output.lower()


def test_readme_rewrite_targets_the_status_column_not_a_title_starting_with_a_status_word(tmp_path):
    """P2 ([26115] #10): a title cell beginning with ``Deferred`` was the
    first cell whose leading word is a status, and was destroyed."""
    from nexus.commands.rdr import _update_readme_status_row

    readme = tmp_path / "README.md"
    readme.write_text(
        "| ID | Title | Status |\n|---|---|---|\n"
        "| [RDR-002](rdr-002-x.md) | Deferred indexing of large trees | Draft |\n"
    )
    dom = frozenset(["draft", "accepted", "deferred", "closed", "superseded", "abandoned"])
    assert _update_readme_status_row(readme, "rdr-002-x.md", "Accepted", dom) is True
    row = readme.read_text().splitlines()[-1]
    assert row == "| [RDR-002](rdr-002-x.md) | Deferred indexing of large trees | Accepted |", row


def test_frontmatter_rewrite_splits_on_fence_lines_only():
    """P8 ([26115] #12): a ``---`` inside a frontmatter value before
    ``status:`` broke the split and the command refused with 'no status key'."""
    from nexus.commands.rdr import _preamble_parse_frontmatter

    text = '---\ntitle: "A --- B"\nstatus: draft\n---\n# body\n'
    out = _rewrite_frontmatter_status(text, "deferred", "2026-09-17")
    assert out == '---\ntitle: "A --- B"\nstatus: deferred\n---\n# body\n'


def test_frontmatter_parse_splits_on_fence_lines_only(tmp_path):
    from nexus.commands.rdr import _preamble_parse_frontmatter

    f = tmp_path / "fm.md"
    f.write_text('---\ntitle: "A --- B"\nstatus: draft\n---\n# body\n')
    meta, _ = _preamble_parse_frontmatter(f)
    assert meta == {"title": "A --- B", "status": "draft"}


def test_rerun_mirror_goes_through_the_table(tmp_path, monkeypatch):
    """A hand-edited file (draft -> closed) is not a decision this command
    made: the re-run mirror advances T2 only along an edge the table admits,
    so it refuses without --reason and completes with one (nexus-nc08w.4)."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 122, "closed")
    project = f"{tmp_path.name}_rdr"
    entries = {(project, "122"): {"title": "122", "content": "status: draft\n"}}
    fake = _FakeT2ReadWriteClient(entries)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    res = _invoke(rdr_dir, "122", "closed")
    assert res.exit_code != 0, res.output
    assert "reason-not-stated" in res.output
    assert entries[(project, "122")]["content"].startswith("status: draft")

    res = _invoke(rdr_dir, "122", "closed", "--reason", "shipped without acceptance", "--date", "2026-09-17")
    assert res.exit_code == 0, res.output
    assert entries[(project, "122")]["content"].startswith("status: closed"), res.output
    assert "closed_date: 2026-09-17" in entries[(project, "122")]["content"]


def test_rerun_mirror_refuses_an_edge_the_table_lacks(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 49, "closed")
    project = f"{tmp_path.name}_rdr"
    entries = {(project, "49"): {"title": "49", "content": "status: abandoned\n"}}
    fake = _FakeT2ReadWriteClient(entries)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    res = _invoke(rdr_dir, "49", "closed", "--reason", "x")
    assert res.exit_code != 0, res.output
    assert "illegal-transition" in res.output
    assert entries[(project, "49")]["content"].startswith("status: abandoned")


def test_mirror_rewrites_every_t2_title_shape(tmp_path, monkeypatch):
    """A record held under "122" and "RDR-122" is one record; mirroring
    only the first shape found left the census reporting it ambiguous
    (live RDR-122, 2026-09-17)."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 122, "closed")
    project = f"{tmp_path.name}_rdr"
    entries = {
        (project, "122"): {"title": "122", "content": "status: closed\n"},
        (project, "RDR-122"): {"title": "RDR-122", "content": "status: draft\n"},
    }
    fake = _FakeT2ReadWriteClient(entries)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    res = _invoke(rdr_dir, "122", "closed", "--reason", "shipped without acceptance")
    assert res.exit_code == 0, res.output
    assert entries[(project, "RDR-122")]["content"].startswith("status: closed"), res.output


def test_flip_rewrites_every_t2_title_shape(tmp_path, monkeypatch):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 210, "accepted")
    project = f"{tmp_path.name}_rdr"
    entries = {
        (project, "210"): {"title": "210", "content": "status: accepted\n"},
        (project, "RDR-210"): {"title": "RDR-210", "content": "status: accepted\n"},
    }
    fake = _FakeT2ReadWriteClient(entries)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    res = _invoke(rdr_dir, "210", "closed", "--date", "2026-09-17")
    assert res.exit_code == 0, res.output
    for title in ("210", "RDR-210"):
        assert entries[(project, title)]["content"].startswith("status: closed"), (title, res.output)


def test_t2_title_shapes_are_distinct():
    """%03d of a three-digit number is the bare number; a duplicated shape
    made the mirror write and report the same title twice (live RDR-122)."""
    assert rdr_mod._t2_rdr_titles(122) == ("122", "RDR-122")
    assert rdr_mod._t2_rdr_titles(42) == ("42", "042", "RDR-42", "RDR-042")


def test_readme_rewrite_scopes_the_status_column_per_table_and_strips_header_decoration(tmp_path):
    """Review of 983f0a0d6: a Status index from an earlier table leaked into
    the next table, whose own header was bold, and the Title cell was
    overwritten while the real Status cell kept Draft."""
    from nexus.commands.rdr import _update_readme_status_row

    readme = tmp_path / "README.md"
    readme.write_text(
        "| Note | Status | Owner |\n|---|---|---|\n| foo | Open | bar |\n\n"
        "| ID | Title | Priority | **Status** |\n|---|---|---|---|\n"
        "| [RDR-002](rdr-002-x.md) | Some Title | High | Draft |\n"
    )
    dom = frozenset(["draft", "accepted", "deferred", "closed", "superseded", "abandoned"])
    assert _update_readme_status_row(readme, "rdr-002-x.md", "Accepted", dom) is True
    assert readme.read_text().splitlines()[-1] == "| [RDR-002](rdr-002-x.md) | Some Title | High | Accepted |"
    assert "| foo | Open | bar |" in readme.read_text()


def test_rerun_mirror_writes_the_files_own_date_not_today(tmp_path, monkeypatch):
    """Review of 983f0a0d6: without --date the completion wrote today's
    date to T2 while the file carried closed_date 2026-06-01."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 999, "closed", extra_fm="closed_date: 2026-06-01\n")
    project = f"{tmp_path.name}_rdr"
    entries = {(project, "999"): {"title": "999", "content": "status: accepted\naccepted_date: 2026-05-01\n"}}
    fake = _FakeT2ReadWriteClient(entries)
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
    res = _invoke(rdr_dir, "999", "closed")
    assert res.exit_code == 0, res.output
    content = entries[(project, "999")]["content"]
    assert "status: closed" in content
    assert "closed_date: 2026-06-01" in content, content


# ---------------------------------------------------------------------------
# nexus-u1jxt.4: resumed work re-gates; nexus-u1jxt.10: escaped pipes
# ---------------------------------------------------------------------------


def test_deferred_to_draft_stamps_resumed_date(tmp_path):
    """The only flip TO draft the table admits is resume; the stamp is what
    the accept guard compares the gate record's date against."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 230, "deferred")
    _write_readme(rdr_dir, 230, "Deferred")
    res = _invoke(rdr_dir, "230", "draft", "--date", "2026-09-18")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "status: draft" in text
    assert "resumed_date: 2026-09-18" in text


def test_accept_is_refused_when_the_gate_record_predates_the_resume(tmp_path, monkeypatch):
    """accepted -> deferred -> draft -> design rewritten -> accepted used to
    succeed on the old PASSED record. Reproduced pre-fix."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 231, "draft", extra_fm="resumed_date: 2026-09-10\n")
    _write_readme(rdr_dir, 231, "Draft")
    project, title = _gate_coords(tmp_path, 231)
    _install_fake_t2(monkeypatch, entries={
        (project, title): {"content": "outcome: PASSED\ndate: 2026-09-01\n"},
        (project, "231"): {"content": "status: draft\nresumed_date: 2026-09-10\n"},
    })
    res = _invoke(rdr_dir, "231", "accepted", "--date", "2026-09-18")
    assert res.exit_code != 0, res.output
    assert "resumed" in res.output and "re-gate" in res.output, res.output


def test_accept_is_refused_for_a_regated_record_without_a_fix_check(tmp_path, monkeypatch):
    """The gate preamble prints "accept refuses this record" for a re-gated
    record with no fix_check; the guard now does refuse it, and admits the
    same record once the field names the commit's own sha."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 232, "draft")
    _write_readme(rdr_dir, 232, "Draft")
    project, title = _gate_coords(tmp_path, 232)
    base = "outcome: PASSED\ndate: 2026-09-18\ncommit: abc1234\nprior: [1] (BLOCKED 1C 0S)\n"
    _install_fake_t2(monkeypatch, entries={(project, title): {"content": base}})
    res = _invoke(rdr_dir, "232", "accepted", "--date", "2026-09-18")
    assert res.exit_code != 0, res.output
    assert "fix_check" in res.output or "fix check" in res.output, res.output

    _install_fake_t2(monkeypatch, entries={
        (project, title): {"content": base + "fix_check: nexus_rdr/232-fix-check-abc1234\n"},
    })
    res = _invoke(rdr_dir, "232", "accepted", "--date", "2026-09-18")
    assert res.exit_code == 0, res.output


def test_readme_row_with_an_escaped_pipe_keeps_its_title(tmp_path):
    """nexus-u1jxt.10: rows were split on every ``|``, so ``\\|`` in a title
    shifted the Status index and the status landed in the title."""
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 233, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    readme = rdr_dir / "README.md"
    readme.write_text(
        "# RDR Index\n\n| RDR | Title | Type | Status | Date |\n|-----|-------|------|--------|------|\n"
        "| [RDR-233](rdr-233-example-title.md) | A \\| B in one title | Architecture | Accepted | 2026-06-22 |\n",
        encoding="utf-8",
    )
    res = _invoke(rdr_dir, "233", "closed", "--date", "2026-09-18")
    assert res.exit_code == 0, res.output
    row = [ln for ln in readme.read_text().splitlines() if "RDR-233" in ln][0]
    assert "A \\| B in one title" in row, row
    assert "| Closed |" in row, row


def test_crlf_file_keeps_crlf_fence_lines_through_the_rewriter():
    """nexus-u1jxt.10: the rewriter re-emitted both fences as bare LF in a
    CRLF file, leaving two odd lines in an otherwise CRLF document."""
    text = "---\r\ntitle: X\r\nstatus: draft\r\n---\r\nbody\r\n"
    out = rdr_mod._rewrite_frontmatter_status(text, "accepted", "2026-09-17")
    assert "\n" not in out.replace("\r\n", ""), out
    assert out.startswith("---\r\n") and "\r\n---\r\nbody" in out, out
    assert "status: accepted\r\n" in out and "accepted_date: 2026-09-17\r\n" in out


def test_reaccept_after_resume_stamps_the_new_accepted_date_on_the_file(tmp_path, monkeypatch):
    """nexus-u1jxt.10 (Sam, 2026-09-18): accepted -> deferred -> draft ->
    accepted kept the file's old accepted_date while T2 took the new one.
    The new date is the acceptance of record on both."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 240, "draft", extra_fm="accepted_date: 2026-01-05\nresumed_date: 2026-09-10\n")
    _write_readme(rdr_dir, 240, "Draft")
    project, title = _gate_coords(tmp_path, 240)
    _install_fake_t2(monkeypatch, entries={(project, title): {"content": "outcome: PASSED\ndate: 2026-09-17\n"}})
    res = _invoke(rdr_dir, "240", "accepted", "--date", "2026-09-17")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "accepted_date: 2026-09-17" in text and "2026-01-05" not in text, text
    assert text.count("accepted_date:") == 1
