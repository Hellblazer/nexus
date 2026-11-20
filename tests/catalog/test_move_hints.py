# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P3-S1: the shared move-hint module (``nexus.catalog.engine_writes.hints``).

Unit tests: no engine, no tenant. The reader is a plain object that answers what the hint asks and records every call,
so a test can say a query was never made. The two lines a hint can print are checked against the command that
``owner_move.py`` actually accepts (the argv parses and carries no write flag), and every query sits inside
``fail_open``."""
from __future__ import annotations

import shlex
from types import SimpleNamespace

import click
import httpx
import pytest
import structlog
from click.testing import CliRunner

from nexus.catalog.engine_writes import hints
from nexus.catalog.tumbler import Tumbler
from nexus.commands.catalog_cmds.owner_move import check_usage  # the command's own usage check, no engine


def _entry(tumbler: str, file_path: str = "", *, alias_of: str = "", meta: dict | None = None, source_uri: str = ""):
    return SimpleNamespace(tumbler=Tumbler.parse(tumbler), file_path=file_path, alias_of=alias_of, meta=meta or {},
                           source_uri=source_uri)


class _Reader:
    """A catalog reader that answers a curator document D (1.1.9 at ``new``) and, by hash, the rows it was given."""

    def __init__(self, *, supported: bool = True, rows=(), here=None, owner_type: str = "curator", boom=None) -> None:
        self.supported = supported
        self.rows = list(rows)
        self.here = here
        self.owner_type = owner_type
        self.boom = boom
        self.calls: list[str] = []

    def supports_catalog_move(self) -> bool:
        self.calls.append("supports_catalog_move")
        return self.supported

    def resolve(self, doc_id):
        self.calls.append("resolve")
        return self.here

    def get_owner_by_prefix(self, prefix):
        self.calls.append("get_owner_by_prefix")
        return {"tumbler_prefix": prefix, "owner_type": self.owner_type}

    def documents_by_meta(self, **kwargs):
        self.calls.append("documents_by_meta")
        if self.boom is not None:
            raise self.boom
        return self.rows, len(self.rows)


@pytest.fixture
def new_pdf(tmp_path):
    path = tmp_path / "moved" / "x.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.4\n")
    return path


# ── the line ────────────────────────────────────────────────────────────────


def test_line_is_dry_run_form(new_pdf, tmp_path):
    """Both hint lines are the dry-run form: no write flag, and the argv is one the real command accepts."""
    doc = hints.document_move_line("1.1.3", str(new_pdf))
    owner = hints.owner_move_line("1.4", str(tmp_path))
    for line in (doc, owner):
        assert "--no-dry-run" not in line and "--confirm" not in line
        argv = shlex.split(line)
        assert argv[:4] == ["nx", "catalog", "owners", "--execute"] and argv[4] == "move"
    assert shlex.split(doc)[5:] == ["--document", "1.1.3", "--new-path", str(new_pdf)]
    assert shlex.split(owner)[5:] == ["--owner", "1.4", "--new-root", str(tmp_path)]

    check_usage("move", "1.1.3", str(new_pdf), None, None, False)  # does not raise
    check_usage("move", None, None, "1.4", str(tmp_path), False)


def test_path_with_space_and_quote_is_shell_quoted(tmp_path):
    awkward = tmp_path / "my docs" / "it's $x; rm.pdf"
    line = hints.document_move_line("1.1.3", str(awkward))
    assert shlex.split(line)[-1] == str(awkward), "a shell reads the path back as the one argument it is"
    root_line = hints.owner_move_line("1.4", str(awkward.parent))
    assert shlex.split(root_line)[-1] == str(awkward.parent)


def test_census_placeholder_is_not_a_path():
    line = hints.owner_move_line("1.4")
    assert line.endswith("--new-root <new root>") and "--no-dry-run" not in line


# ── fail open ───────────────────────────────────────────────────────────────


def test_query_exception_is_logged_and_dropped():
    def refuse():
        raise RuntimeError("the engine said no")

    with structlog.testing.capture_logs() as logs:
        assert hints.fail_open("curator", refuse, default=["kept"]) == ["kept"]
    dropped = [e for e in logs if e["event"] == "move_hints.query_dropped"]
    assert len(dropped) == 1 and dropped[0]["hint"] == "curator" and dropped[0]["error_type"] == "RuntimeError"


def test_query_timeout_is_logged_and_dropped(new_pdf):
    """A transport timeout from the metadata query is dropped by the wrapper, so the hint prints nothing and the caller
    sees no exception; a spent time budget reads as spent once its seconds pass."""
    here = _entry("1.1.9", str(new_pdf))
    reader = _Reader(here=here, boom=httpx.ReadTimeout("read timed out"))
    with structlog.testing.capture_logs() as logs:
        with hints.run_scope() as scope:
            hints.record_curator_hint("1.1.9", "a" * 64, reader=reader)
            assert scope.drain() == []
    assert any(e["event"] == "move_hints.query_dropped" and e["error_type"] == "ReadTimeout" for e in logs)

    ticks = iter([0.0, 1.0, 99.0, 99.0, 99.0])
    budget = hints.Budget(5.0, clock=lambda: next(ticks))
    assert budget.spent() is False and budget.spent() is True


def test_probe_that_raises_reads_as_no_support():
    class _Bad:
        def supports_catalog_move(self):
            raise RuntimeError("version probe failed")

    assert hints.engine_supports_moves(_Bad()) is False
    assert hints.engine_supports_moves(object()) is False


# ── the probe guard ─────────────────────────────────────────────────────────


def test_no_hint_on_engine_without_flag(new_pdf):
    reader = _Reader(supported=False, here=_entry("1.1.9", str(new_pdf)), rows=[_entry("1.1.3", "/gone/x.pdf")])
    assert hints.curator_hint_lines(reader, "1.1.9", "a" * 64) == []
    assert reader.calls == ["supports_catalog_move"], "no catalog read once the probe says no"
    assert "documents_by_meta" not in reader.calls


def test_curator_line_for_an_absent_old_row_only(new_pdf, tmp_path):
    present = tmp_path / "still" / "x.pdf"
    present.parent.mkdir()
    present.write_bytes(b"x")
    reader = _Reader(
        here=_entry("1.1.9", str(new_pdf)),
        rows=[
            _entry("1.1.9", str(new_pdf)),                      # D itself
            _entry("1.1.3", str(tmp_path / "gone" / "x.pdf")),   # file absent: hint
            _entry("1.1.4", str(present)),                       # file present: a copy
            _entry("1.1.5", str(tmp_path / "gone2.pdf"), alias_of="1.1.3"),  # an alias row
            _entry("1.2.1", str(tmp_path / "gone3.pdf")),        # another owner
        ],
    )
    assert hints.curator_hint_lines(reader, "1.1.9", "a" * 64) == [hints.document_move_line("1.1.3", str(new_pdf))]


# ── the collector ───────────────────────────────────────────────────────────


def test_collector_prints_once_at_end():
    collector = hints.HintCollector()
    collector.add("a")
    collector.add("b")
    collector.add("a")
    assert collector.drain() == ["a", "b"] and collector.drain() == []

    @click.command()
    @hints.prints_hints
    def cmd():
        click.echo("indexing...")
        hints.active_collector().add("the hint")
        hints.active_collector().add("the hint")
        click.echo("Done.")

    result = CliRunner().invoke(cmd)
    assert result.output.splitlines() == ["indexing...", "Done.", "the hint"], "one line, after the command's own output"
    assert hints.active_collector() is None, "the scope closed with the command"


def test_a_failing_command_prints_no_hint_and_closes_the_scope():
    @click.command()
    @hints.prints_hints
    def cmd():
        hints.active_collector().add("the hint")
        raise click.ClickException("the run failed")

    result = CliRunner().invoke(cmd)
    assert result.exit_code == 1 and "the hint" not in result.output
    assert hints.active_collector() is None


def test_record_without_an_open_scope_queries_nothing(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)))
    hints.record_curator_hint("1.1.9", "a" * 64, reader=reader)
    assert reader.calls == []
