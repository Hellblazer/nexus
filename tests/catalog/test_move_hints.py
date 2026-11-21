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


class _ByHash(_Reader):
    """A reader whose metadata route answers, per hash, the tumblers given in ``owners_by_hash``."""

    def __init__(self, owners_by_hash: dict[str, list[str]]) -> None:
        super().__init__()
        self.owners_by_hash = owners_by_hash
        self.asked: list[str] = []

    def documents_by_meta(self, **kwargs):
        h = kwargs["meta"][0][1]
        self.asked.append(h)
        rows = [_entry(t, "/gone") for t in self.owners_by_hash.get(h, [])]
        return rows, len(rows)


def _hashed_page(*hashes: str) -> list:
    return [_entry(f"1.2.{i + 1}", meta={"content_hash": h}) for i, h in enumerate(hashes)]


def test_rename_loop_stops_asking_once_the_budget_is_spent_and_returns_the_matches_found():
    """The budget runs out after two of four hashes: the owner that matched both of the two asked is still named
    (two matches, all of what was asked), and the last two hashes are never asked."""
    reader = _ByHash({h: ["1.7.1"] for h in ("h1", "h2", "h3", "h4")})
    spent = iter([False, False, True, True])
    stop = SimpleNamespace(spent=lambda: next(spent))
    lines = hints.rename_hint_lines(reader, "1.2", "/new", _hashed_page("h1", "h2", "h3", "h4"), ["1.7"], stop)
    assert reader.asked == ["h1", "h2"]
    assert lines == [hints.owner_move_line("1.7", "/new")]


def test_rename_needs_at_least_two_matching_hashes():
    one_doc = _hashed_page("h1")
    assert hints.rename_hint_lines(_ByHash({"h1": ["1.7.1"]}), "1.2", "/new", one_doc, ["1.7"], hints.Budget()) == [], \
        "one hashed document and one match is not evidence of a rename"
    two_docs = _hashed_page("h1", "h2")
    assert hints.rename_hint_lines(_ByHash({"h1": ["1.7.1"]}), "1.2", "/new", two_docs, ["1.7"], hints.Budget()) == [], \
        "half of two is one match, still below the floor of two"
    both = _ByHash({"h1": ["1.7.1"], "h2": ["1.7.2"]})
    assert hints.rename_hint_lines(both, "1.2", "/new", two_docs, ["1.7"], hints.Budget()) == [hints.owner_move_line("1.7", "/new")]


def test_rename_ties_break_by_tumbler_order_not_lexical_order():
    reader = _ByHash({"h1": ["1.10.1", "1.9.1", "1.3.8"], "h2": ["1.10.2", "1.9.2"]})
    lines = hints.rename_hint_lines(reader, "1.2", "/new", _hashed_page("h1", "h2"), ["1.10", "1.9", "1.3"], hints.Budget())
    assert lines == [hints.owner_move_line("1.9", "/new"), hints.owner_move_line("1.10", "/new")], \
        "1.9 sorts before 1.10 as tumblers do (lexically 1.10 would come first), and 1.3, with one match, is out"


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


def test_a_command_that_fails_with_a_click_exception_still_prints_its_hints():
    """A ``--dir`` batch that ends ``N of M failed`` has already written the files that succeeded; the hints describe
    those writes, and the curator hint is not repeated on a later run. The error message follows them."""

    @click.command()
    @hints.prints_hints
    def cmd():
        click.echo("indexing...")
        hints.active_collector().add("the hint")
        raise click.ClickException("1 of 2 file(s) failed")

    result = CliRunner().invoke(cmd)
    assert result.exit_code == 1
    assert result.output.splitlines()[:2] == ["indexing...", "the hint"] and "1 of 2 file(s) failed" in result.output
    assert hints.active_collector() is None


def test_an_unexpected_exception_prints_no_hint_and_closes_the_scope():
    """Anything that is not a ClickException (a bug, an interrupt) says nothing is known about what was written, so
    no hint is printed; the scope still closes."""

    @click.command()
    @hints.prints_hints
    def cmd():
        hints.active_collector().add("the hint")
        raise RuntimeError("boom")

    result = CliRunner().invoke(cmd)
    assert result.exit_code == 1 and isinstance(result.exception, RuntimeError) and "the hint" not in result.output
    assert hints.active_collector() is None


def test_record_without_an_open_scope_queries_nothing(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)))
    hints.record_curator_hint("1.1.9", "a" * 64, reader=reader)
    assert reader.calls == []


# ── the curator hint's cost per run ─────────────────────────────────────────


def _record_many(reader, n: int, scope_kwargs: dict | None = None) -> None:
    with hints.run_scope(**(scope_kwargs or {})):
        for i in range(n):
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)


def test_curator_hint_stops_asking_after_the_first_failed_query(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)), boom=httpx.ReadTimeout("read timed out"))
    _record_many(reader, 5)
    assert reader.calls.count("documents_by_meta") == 1, "one failed query ends the asking for the rest of the run"
    calls_after_first = len(reader.calls)
    reader.calls.clear()
    _record_many(reader, 1)
    assert calls_after_first >= 1 and reader.calls != [], "a new run asks again (the stop is per run, not per process)"


def test_curator_hint_asks_about_a_capped_number_of_files_per_run(new_pdf, monkeypatch):
    monkeypatch.setattr(hints, "CURATOR_HINT_MAX_FILES", 3)
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)))
    _record_many(reader, 10)
    assert reader.calls.count("documents_by_meta") == 3


def test_curator_hint_spends_a_run_wide_time_budget(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)))
    _record_many(reader, 4, {"budget_s": 0.0})
    assert reader.calls == [], "a spent budget asks nothing, not even the probe"


def test_the_owner_kind_is_read_once_per_owner_per_run(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)))
    _record_many(reader, 4)
    assert reader.calls.count("get_owner_by_prefix") == 1 and reader.calls.count("documents_by_meta") == 4


class _Clock:
    """An injected clock: time moves only when the test (or a reader call) moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _SlowReader(_Reader):
    """Every metadata query takes ``cost`` seconds of the injected clock."""

    def __init__(self, clock: _Clock, cost: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clock, self.cost = clock, cost

    def documents_by_meta(self, **kwargs):
        self.clock.now += self.cost
        return super().documents_by_meta(**kwargs)


def test_time_before_the_first_stamp_does_not_spend_the_budget(new_pdf):
    """GH #1571 shape: extraction and embedding take longer than the budget before the writer first stamps. The budget
    counts time spent in hint queries, not the command's wall clock, so the first file is still asked about and the
    hint prints."""
    clock = _Clock()
    old = _entry("1.1.3", "/gone/x.pdf")
    reader = _SlowReader(clock, 0.1, here=_entry("1.1.9", str(new_pdf)), rows=[old])
    with hints.run_scope(budget_s=30.0, clock=clock) as scope:
        clock.now += 600.0  # ten minutes of MinerU before the first stamp
        hints.record_curator_hint("1.1.9", "a" * 64, reader=reader)
        lines = scope.drain()
    assert reader.calls.count("documents_by_meta") == 1
    assert lines == [hints.document_move_line("1.1.3", str(new_pdf))]


def test_time_between_files_does_not_spend_the_budget_but_query_time_does(new_pdf):
    """A ``--dir`` batch spends long stretches between files; only the seconds inside hint queries add up."""
    clock = _Clock()
    reader = _SlowReader(clock, 4.0, here=_entry("1.1.9", str(new_pdf)))
    with hints.run_scope(budget_s=10.0, clock=clock):
        for i in range(6):
            clock.now += 300.0  # five minutes of indexing between two files
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)
    assert reader.calls.count("documents_by_meta") == 3, "4 s a file: three files fit under 10 s, the fourth finds it spent"


# ── the closing line when files were not checked ────────────────────────────


_NOT_CHECKED = "move hints checked for {n} of {m} files; run nx index pdf --force on a moved file to check it again"


def test_a_capped_run_ends_with_one_line_saying_how_many_files_were_checked(new_pdf, monkeypatch):
    monkeypatch.setattr(hints, "CURATOR_HINT_MAX_FILES", 3)
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)), rows=[_entry("1.1.3", "/gone/x.pdf")])
    with hints.run_scope() as scope:
        for i in range(10):
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)
        lines = scope.drain()
    assert lines[-1] == _NOT_CHECKED.format(n=3, m=10)
    assert lines[:-1] == [hints.document_move_line("1.1.3", str(new_pdf))], "the move line first, once"
    assert not lines[-1].startswith("nx catalog") and "--no-dry-run" not in lines[-1] and "--confirm" not in lines[-1]
    assert scope.drain() == [], "printed once"


def test_a_run_stopped_by_a_failed_query_ends_with_the_same_line(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)), boom=httpx.ReadTimeout("read timed out"))
    with hints.run_scope() as scope:
        for i in range(5):
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)
        lines = scope.drain()
    assert lines == [_NOT_CHECKED.format(n=0, m=5)]


def test_a_capped_run_on_an_engine_without_the_move_routes_prints_nothing(new_pdf, monkeypatch):
    """The probe-first rule: an engine that reports no move routes gets no hint output at all, not a "checked N of
    M" note for files the hint never read (confirmation round r3, R-a)."""
    monkeypatch.setattr(hints, "CURATOR_HINT_MAX_FILES", 2)
    reader = _Reader(supported=False, here=_entry("1.1.9", str(new_pdf)), rows=[_entry("1.1.3", "/gone/x.pdf")])
    with hints.run_scope() as scope:
        for i in range(3):
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)
        lines = scope.drain()
    assert lines == []
    assert set(reader.calls) == {"supports_catalog_move"}, "nothing but the probe was asked"


def test_a_spent_budget_ends_with_the_same_line(new_pdf):
    clock = _Clock()
    reader = _SlowReader(clock, 6.0, here=_entry("1.1.9", str(new_pdf)))
    with hints.run_scope(budget_s=10.0, clock=clock) as scope:
        for i in range(4):
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)
        lines = scope.drain()
    assert lines == [_NOT_CHECKED.format(n=2, m=4)]


def test_a_run_that_checked_every_file_ends_with_no_such_line(new_pdf):
    reader = _Reader(here=_entry("1.1.9", str(new_pdf)))
    with hints.run_scope() as scope:
        for i in range(3):
            hints.record_curator_hint(f"1.1.{i + 10}", "a" * 64, reader=reader)
        assert scope.drain() == []


# ── the census flag on a page of malformed entries ──────────────────────────


class _BadDoc:
    file_path = "src/a.py"
    source_uri = 12345


class _GoodDoc:
    file_path = "src/a.py"
    source_uri = "file:///r/src/a.py"


def test_a_page_of_only_malformed_entries_reads_unknown_not_false():
    class _Page:
        def owner_documents_page(self, owner, limit):
            return [_BadDoc(), _BadDoc()]

    assert hints.count_docs_outside_root([_BadDoc(), _BadDoc()], "/r") is None
    assert hints.docs_outside_root_flag(_Page(), "1.2", "/r") is None, "unknown, not 'no document is outside'"
    assert hints.count_docs_outside_root([_BadDoc(), _GoodDoc()], "/r") == 0, "one readable entry: a known zero"
    assert hints.count_docs_outside_root([], "/r") == 0, "an empty page is a known zero"
