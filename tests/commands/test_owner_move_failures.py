# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P2-S5 review round: what ``nx catalog owners --execute move`` says when a route call fails, what its
report prints, and the structural rule that no move route is reached before the capability check.

The failures are driven through ``CliRunner`` with a fake catalog handle (no engine): the engine's own refusals are
pinned Java-side and in tests/commands/test_catalog_owners_move.py against the substrate.
"""
from __future__ import annotations

import ast
import shlex
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import catalog as _cat_cmd

_REPORT = {
    "dry_run": False, "repointed": 1, "absorbed": 1, "trashed": 0, "skipped": 0, "unchanged": 0,
    "aspect_rows_rewritten": 0, "aspect_rows_deleted": 0, "highlight_rows_rewritten": 0, "highlight_rows_deleted": 0,
    "links_remapped": 0, "links_collapsed": 0, "links_dropped": 0, "absorbed_tumblers": ["1.2.9"],
}


class _Reader:
    def catalog_move_probe(self):
        return True, "0.1.199"


class _Writer:
    def __init__(self, behaviour) -> None:
        self.behaviour, self.calls, self.closed = behaviour, [], False

    def move_document(self, tumbler, **kw):
        self.calls.append(("document", tumbler, kw))
        return self.behaviour(kw)

    def move_owner(self, owner, **kw):
        self.calls.append(("owner", owner, kw))
        return self.behaviour(kw)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pdf(tmp_path) -> Path:
    path = tmp_path / "new" / "x.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.4\n")
    return path


@pytest.fixture
def writer(monkeypatch):
    """Install a fake reader and a writer whose move call runs ``holder.behaviour``."""
    holder = _Writer(lambda kw: {**_REPORT, "dry_run": kw["dry_run"]})
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: _Reader())
    monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda: holder)
    return holder


def _move(pdf: Path, *flags: str):
    return CliRunner().invoke(main, ["catalog", "owners", "--execute", "move", "--document", "1.2.3",
                                     "--new-path", str(pdf), *flags])


def _status(code: int, body=None, text: str = ""):
    request = httpx.Request("POST", "http://engine.test/v1/catalog/documents/move")
    response = httpx.Response(code, json=body, request=request) if body is not None else httpx.Response(
        code, text=text, request=request)
    return httpx.HTTPStatusError(f"Server error '{code}' for url", request=request, response=response)


# ── a failed write says what is known ───────────────────────────────────────


@pytest.mark.parametrize("code", [400, 409])
def test_a_400_or_409_says_nothing_was_written(writer, pdf, code) -> None:
    def refuse(kw):
        raise _status(code, {"error": "another live row holds file:///new/x.pdf"})

    writer.behaviour = refuse
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "another live row holds" in result.output and "Nothing was written" in result.output
    assert "unknown" not in result.output.lower()


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_any_other_status_says_the_outcome_is_unknown_not_nothing_written(writer, pdf, code) -> None:
    def broken(kw):
        raise _status(code, text="upstream gateway message")

    writer.behaviour = broken
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "outcome is unknown" in result.output and "dry run" in result.output and "re-running is safe" in result.output
    assert str(code) in result.output, "str(exc) survives, so an edge refusal's message is not lost"
    assert "Nothing was written" not in result.output


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("reset"), httpx.WriteTimeout("slow")])
def test_a_timeout_or_lost_connection_on_the_write_says_it_may_have_completed(writer, pdf, exc) -> None:
    def lost(kw):
        raise exc

    writer.behaviour = lost
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and result.exception is not None and isinstance(result.exception, SystemExit)
    assert "may have completed server-side" in result.output and "re-running is safe" in result.output
    assert "without --no-dry-run --confirm" in result.output and "Traceback" not in result.output
    assert "Nothing was written" not in result.output
    assert len(writer.calls) == 1 and writer.closed


def test_a_timeout_on_a_dry_run_says_nothing_was_written_by_it(writer, pdf) -> None:
    def lost(kw):
        raise httpx.ReadTimeout("slow")

    writer.behaviour = lost
    result = _move(pdf)
    assert result.exit_code != 0 and "dry run writes nothing" in result.output
    assert "may have completed" not in result.output


def test_a_400_names_the_engines_field_and_value_and_a_non_object_body_is_tolerated(writer, pdf) -> None:
    writer.behaviour = lambda kw: (_ for _ in ()).throw(
        _status(400, {"error": "tumbler grammar violation", "field": "tumbler", "value": "1"}))
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert "(field tumbler: 1)" in result.output and "Nothing was written" in result.output

    for body in ([1, 2], "just a string", None):
        writer.behaviour = lambda kw, b=body: (_ for _ in ()).throw(_status(409, b, text="plain refusal"))
        again = _move(pdf, "--no-dry-run", "--confirm")
        assert again.exit_code != 0 and "Nothing was written" in again.output and "Traceback" not in again.output


def test_a_unique_index_409_says_to_run_it_again(writer, pdf) -> None:
    writer.behaviour = lambda kw: (_ for _ in ()).throw(_status(409, {"error": "integrity constraint violation"}))
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert "Nothing was written. Run it again." in result.output


def test_an_answer_that_contradicts_the_request_is_an_error_not_done(writer, pdf) -> None:
    writer.behaviour = lambda kw: {**_REPORT, "dry_run": True}  # a write was asked for; the engine says it only rehearsed
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "Done." not in result.output and "refusing to report a move" in result.output
    writer.behaviour = lambda kw: {**_REPORT, "dry_run": False}  # a dry run was asked for; the engine says it wrote
    result = _move(pdf)
    assert result.exit_code != 0 and "refusing to report a move" in result.output
    writer.behaviour = lambda kw: {k: v for k, v in _REPORT.items() if k != "dry_run"}
    assert _move(pdf).exit_code != 0, "an answer with no dry_run flag proves nothing"


# ── the report ──────────────────────────────────────────────────────────────


def test_the_write_form_prints_the_resolved_path_never_the_typed_one(writer, pdf, tmp_path) -> None:
    link = tmp_path / "linked"
    link.symlink_to(pdf.parent, target_is_directory=True)
    typed = link / ".." / "linked" / "x.pdf"
    result = _move(typed)
    assert result.exit_code == 0, result.output
    last = result.output.rstrip("\n").splitlines()[-1]
    assert shlex.split(last)[shlex.split(last).index("--new-path") + 1] == str(pdf.resolve())
    assert str(typed) not in last


def test_a_dry_run_says_would_be_trashed_and_a_write_says_now_trashed(writer, pdf) -> None:
    assert "absorbed documents (1, would be trashed):\n    1.2.9" in _move(pdf).output
    assert "absorbed documents (1, now trashed):\n    1.2.9" in _move(pdf, "--no-dry-run", "--confirm").output


def test_the_orphan_trash_row_is_an_owner_move_row_only(writer, pdf, tmp_path) -> None:
    assert "trashed (folded owner's rows" not in _move(pdf).output
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    out = CliRunner().invoke(main, ["catalog", "owners", "--execute", "move", "--owner", "1.5", "--new-root", str(repo)])
    assert out.exit_code == 0, out.output
    assert "trashed (folded owner's rows with no counterpart): 0" in out.output


def test_no_dry_run_without_confirm_does_not_say_owners_are_mutated(writer, pdf) -> None:
    result = _move(pdf, "--no-dry-run")
    assert "Add --confirm to actually move." in result.output and "mutate owners" not in result.output
    assert writer.calls[0][2]["dry_run"] is True


# ── no move route before the capability check ───────────────────────────────

_SRC = Path(__file__).resolve().parents[2] / "src" / "nexus"
_MOVE_METHODS = {"move_document", "move_owner"}


def _calls(tree: ast.AST, names: set[str]) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Attribute) and n.func.attr in names)
                 or (isinstance(n.func, ast.Name) and n.func.id in names))]


def test_only_owner_move_calls_a_move_route_and_the_capability_check_comes_first() -> None:
    examined, callers = 0, {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        examined += 1
        hits = _calls(tree, _MOVE_METHODS)
        if hits:
            callers[path.relative_to(_SRC).as_posix()] = (tree, hits)
    print(f"examined {examined} source files; callers: {sorted(callers)}")
    assert examined > 500, "the scan did not walk the source tree"
    assert set(callers) == {"commands/catalog_cmds/owner_move.py"}, sorted(callers)
    tree, hits = callers["commands/catalog_cmds/owner_move.py"]
    run_move = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_move")
    inside = [c for c in hits if run_move.lineno <= c.lineno <= (run_move.end_lineno or 0)]
    assert len(inside) == len(hits) == 2, "both move calls sit in run_move"
    check = _calls(run_move, {"require_catalog_move"})
    assert len(check) == 1 and check[0].lineno < min(c.lineno for c in inside)
