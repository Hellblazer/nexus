# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 Technical Design 1, display.threads (nexus-mdbds.11.1 part F, rules
15 to 22): the host finds the entry by its session: line through the board's
own reader, never creates one, refuses to guess between two, writes one
heading and one capped state: line, and replaces or removes only its block."""

from __future__ import annotations

import threading

import pytest

from nexus.auras.threads_block import ThreadsBlockWriter, sanitize_state

_SID = "11111111-2222-3333-4444-555555555555"
_ENTRY = f"session: {_SID}\n\n## Some work\nref: nexus-x\nstate: going\nwaiting_on: none\nneeds_sam: none\n"


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
    return ThreadsBlockWriter(_SID, read_entries=io.read, put=io.put)


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


def test_the_entry_is_found_by_its_session_line_never_by_title() -> None:
    io = _Io([
        {"title": "tamsin", "content": "session: someone-else\n", "tags": ""},
        {"title": "handle-7", "content": _ENTRY, "tags": ""},
    ])
    assert _writer(io).set_block("a", "x", "y")
    assert [t for t, _c, _g in io.puts] == ["handle-7"]


def test_no_entry_carrying_the_line_writes_nothing() -> None:
    io = _Io([{"title": "tamsin", "content": "no session line here\n", "tags": ""}])
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
