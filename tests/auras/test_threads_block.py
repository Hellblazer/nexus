# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 Technical Design 1, display.threads (nexus-mdbds.11.1 part F, rules
15 to 22): the host finds the entry by its identity: line (RDR-253 Phase 2: never
the session: line) through the board's own reader, never creates one, refuses to guess between two, writes one
heading and one capped state: line, and replaces or removes only its block."""

from __future__ import annotations

import threading

import structlog

import pytest

from nexus.auras.threads_block import ThreadsBlockWriter, sanitize_state

_SID = "11111111-2222-3333-4444-555555555555"
_IDENT = "i12"
_ENTRY = f"identity: {_IDENT}\n\n## Some work\nref: nexus-x\nstate: going\nwaiting_on: none\nneeds_sam: none\n"


class _Io:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = {r["title"]: dict(r) for r in rows}
        self.puts: list[tuple[str, str, str]] = []

    def read(self) -> list[dict]:
        return [dict(r) for r in self.rows.values()]

    def put(self, title: str, content: str, tags: str) -> None:
        self.puts.append((title, content, tags))
        self.rows[title] = {"title": title, "content": content, "tags": tags}


def _writer(io: _Io) -> ThreadsBlockWriter:
    return ThreadsBlockWriter(_SID, read_entries=io.read, put=io.put, identity=_IDENT)


def test_a_block_is_one_heading_and_one_state_line_appended_to_the_session_entry() -> None:
    io = _Io([{"title": "tamsin", "content": _ENTRY, "tags": "board"}])
    assert _writer(io).set_block("ci_develop_red", "develop CI red", "develop red at abc1234")
    title, content, tags = io.puts[-1]
    assert title == "tamsin" and tags == "board"
    assert content.startswith(_ENTRY.rstrip("\n"))
    assert content.rstrip("\n").endswith("## aura: develop CI red\nstate: develop red at abc1234")


def test_the_latest_fire_replaces_only_its_own_block() -> None:
    io = _Io([{"title": "tamsin", "content": _ENTRY, "tags": ""}])
    w = _writer(io)
    w.set_block("a", "develop CI red", "first")
    w.set_block("b", "other aura", "other")
    w.set_block("a", "develop CI red", "second")
    content = io.rows["tamsin"]["content"]
    assert content.count("## aura: develop CI red") == 1
    assert "state: second" in content and "state: first" not in content
    assert "## aura: other aura\nstate: other" in content
    assert "## Some work\nref: nexus-x" in content


def test_the_entry_is_found_by_its_identity_line_never_by_title() -> None:
    io = _Io([
        {"title": "tamsin", "content": "identity: i7\n", "tags": ""},
        {"title": "handle-7", "content": _ENTRY, "tags": ""},
    ])
    assert _writer(io).set_block("a", "x", "y")
    assert [t for t, _c, _g in io.puts] == ["handle-7"]


def test_no_entry_carrying_the_line_writes_nothing() -> None:
    io = _Io([{"title": "tamsin", "content": "no identity line here\n", "tags": ""}])
    assert not _writer(io).set_block("a", "x", "y")
    assert io.puts == []


def test_two_entries_carrying_the_line_write_nothing() -> None:
    io = _Io([{"title": "one", "content": _ENTRY, "tags": ""}, {"title": "two", "content": _ENTRY, "tags": ""}])
    assert not _writer(io).set_block("a", "x", "y")
    assert io.puts == []


def test_a_clear_removes_the_block_and_a_clear_with_no_block_writes_nothing() -> None:
    io = _Io([{"title": "tamsin", "content": _ENTRY, "tags": ""}])
    w = _writer(io)
    assert not w.remove_block("a", "develop CI red")
    assert io.puts == []
    w.set_block("a", "develop CI red", "red")
    assert w.remove_block("a", "develop CI red")
    content = io.rows["tamsin"]["content"]
    assert "aura:" not in content and "## Some work" in content


def test_procedure_text_cannot_open_a_heading_an_ask_or_another_field() -> None:
    io = _Io([{"title": "tamsin", "content": _ENTRY, "tags": ""}])
    text = "red\n## injected\nask: evil\nneeds_sam: yes\t\x1b[31m" + "z" * 400
    _writer(io).set_block("a", "develop CI red", text)
    block = io.rows["tamsin"]["content"].split("## aura: develop CI red\n", 1)[1]
    lines = [ln for ln in block.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("state: ")
    assert len(lines[0]) == len("state: ") + 200


@pytest.mark.parametrize(
    ("raw", "want"),
    [("a\nb\tc", "a b c"), ("café", "caf"), ("x\x00y\x7fz", "xyz"), ("q" * 250, "q" * 200)],
)
def test_sanitize_state(raw: str, want: str) -> None:
    assert sanitize_state(raw) == want


def test_every_read_modify_write_holds_one_lock() -> None:
    """Two writers for two auras in one server never interleave a read and a write."""
    io = _Io([{"title": "tamsin", "content": _ENTRY, "tags": ""}])
    inside = threading.Event()
    release = threading.Event()
    order: list[str] = []
    real_read = io.read

    def slow_read() -> list[dict]:
        rows = real_read()
        if not inside.is_set():
            inside.set()
            release.wait(2)
        order.append("read")
        return rows

    io.read = slow_read  # type: ignore[method-assign]
    a, b = _writer(io), _writer(io)
    t = threading.Thread(target=a.set_block, args=("a", "aura a", "one"))
    t.start()
    inside.wait(2)
    u = threading.Thread(target=b.set_block, args=("b", "aura b", "two"))
    u.start()
    u.join(0.2)
    assert u.is_alive(), "the second writer must wait for the first's write"
    release.set()
    t.join(2)
    u.join(2)
    content = io.rows["tamsin"]["content"]
    assert "## aura: aura a\nstate: one" in content and "## aura: aura b\nstate: two" in content


def test_no_entry_and_two_entries_log_the_aura_and_the_titles() -> None:
    with structlog.testing.capture_logs() as logs:
        _writer(_Io([])).set_block("red", "x", "y")
        _writer(_Io([{"title": "two", "content": _ENTRY, "tags": ""}, {"title": "one", "content": _ENTRY, "tags": ""}])).set_block(
            "red", "x", "y")
    (none,) = [e for e in logs if e["event"] == "aura_threads_no_entry"]
    assert none["aura"] == "red" and none["log_level"] == "warning"
    (both,) = [e for e in logs if e["event"] == "aura_threads_ambiguous"]
    assert both["aura"] == "red" and both["titles"] == ["one", "two"]


def test_a_clear_keeps_text_an_agent_wrote_after_the_block() -> None:
    io = _Io([{"title": "tamsin", "content": _ENTRY, "tags": ""}])
    w = _writer(io)
    w.set_block("a", "develop CI red", "red")
    io.rows["tamsin"]["content"] += "an agent's own note under the block\n"
    assert w.remove_block("a", "develop CI red")
    content = io.rows["tamsin"]["content"]
    assert "aura:" not in content and "state: red" not in content
    assert "an agent's own note under the block" in content and "## Some work\nref: nexus-x" in content


# --- RDR-253 Phase 2: the aura threads block reads the identity line, and only it -----------------------


def test_the_block_goes_to_the_entry_whose_identity_line_names_the_seat_after_clear() -> None:
    stale = "99999999-8888-4777-8666-555555555555"  # the session id the entry was written under, before /clear
    io = _Io([{"title": "tamsin", "content": f"session: {stale}\n" + _ENTRY, "tags": "board"}])
    writer = ThreadsBlockWriter(_SID, read_entries=io.read, put=io.put, identity=_IDENT)
    assert writer.set_block("ci_develop_red", "develop CI red", "develop red at abc1234")
    assert io.puts and io.puts[0][0] == "tamsin"


def test_an_identity_only_entry_is_found_and_a_session_only_entry_is_not() -> None:
    only_identity = _Io([{"title": "tamsin", "content": _ENTRY, "tags": "board"}])
    assert "session:" not in _ENTRY and _writer(only_identity).set_block("a", "A", "x")
    older = _Io([{"title": "tamsin", "content": _ENTRY.replace(f"identity: {_IDENT}", f"session: {_SID}"), "tags": "board"}])
    assert not _writer(older).set_block("a", "A", "x"), "the session: line is not a key"
    assert older.puts == []


def test_an_entry_owned_by_another_identity_is_never_written_and_an_older_twin_does_not_make_it_ambiguous() -> None:
    io = _Io([{"title": "other", "content": f"session: {_SID}\nidentity: i7\n" + _ENTRY.split("\n", 1)[1], "tags": "board"}])
    assert not _writer(io).set_block("a", "A", "x")
    assert io.puts == []
    io = _Io([
        {"title": "tamsin", "content": _ENTRY, "tags": "board"},
        {"title": "tamsin-old", "content": _ENTRY.replace(f"identity: {_IDENT}", f"session: {_SID}"), "tags": "board"},
    ])
    assert _writer(io).set_block("a", "A", "x")
    assert [p[0] for p in io.puts] == ["tamsin"]


def test_a_writer_built_before_the_binding_asks_for_the_identity_at_write_time() -> None:
    stale = "99999999-8888-4777-8666-555555555555"
    io = _Io([{"title": "tamsin", "content": f"identity: {_IDENT}\nsession: {stale}\n## W\nstate: x\n", "tags": "board"}])
    known: list[str] = [""]
    writer = ThreadsBlockWriter(_SID, read_entries=io.read, put=io.put, identity_source=lambda: known[0])
    assert not writer.set_block("a", "A", "x"), "no identity yet, so no entry is ours"
    known[0] = _IDENT
    assert writer.set_block("a", "A", "x") and io.puts[-1][0] == "tamsin"
