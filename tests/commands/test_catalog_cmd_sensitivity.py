# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog show``, ``list`` and ``search`` output posture for sensitive documents (RDR-243 Q2).

Q2 (decided): output read through Claude Code's Bash tool reaches the session model, so under Claude Code
(``nexus.session.started_by_claude_code``) a sensitive document's row is its stub (the tumbler and the non-content
columns, with the stub line), counted; in a terminal the row prints whole with a ``[sensitive]`` badge. The pieces
are ``nexus.sensitive_output``'s document-row helpers (nexus-rfvyy.1), the posture is the CLI's, not the config's.

Tenant-free: a fake catalog stands in for the engine's, because what is under test is how a row is rendered,
and the canary in its title, path, URI, author and link meta must not reach stdout when withheld.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, CatalogLink
from nexus.cli import main
from nexus.context_annotations import SENSITIVE_READER_CLAUSE
from nexus.sensitive_output import BADGE, stub_line, withheld_note

CANARY = "CANARY-catalogcli-5521"
SECRET_TITLE = f"{CANARY} board minutes"
SECRET_PATH = f"/private/{CANARY}/minutes.md"
SECRET_URI = f"file://{SECRET_PATH}"
SECRET_AUTHOR = f"Author {CANARY}"
SECRET_SPAN_TEXT = f"{CANARY} span text from the sensitive document"
SECRET_LINK_NOTE = f"{CANARY} link note"

CLEAN_A, SECRET, CLEAN_B = "1.1.1", "1.1.2", "1.1.3"


def _entry(tumbler: str, title: str, *, sensitive: bool = False, path: str = "", uri: str = "", author: str = "") -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title, author=author, year=2020, content_type="knowledge",
        file_path=path, corpus="c", physical_collection="knowledge__c__m__v1", chunk_count=3, head_hash="h",
        indexed_at="2026-01-01T00:00:00Z", source_uri=uri, sensitive=sensitive,
    )


class _FakeCatalog:
    def __init__(self) -> None:
        self.entries = [
            _entry(CLEAN_A, "Gardening", path="/pub/garden.md"),
            _entry(SECRET, SECRET_TITLE, sensitive=True, path=SECRET_PATH, uri=SECRET_URI, author=SECRET_AUTHOR),
            _entry(CLEAN_B, "Cooking", path="/pub/cook.md"),
        ]
        self.links = [
            CatalogLink(from_tumbler=Tumbler.parse(CLEAN_A), to_tumbler=Tumbler.parse(SECRET), link_type="cites",
                        from_span="", to_span="chash:" + "a" * 64, created_by="t", created_at="", meta={"note": SECRET_LINK_NOTE}),
            CatalogLink(from_tumbler=Tumbler.parse(CLEAN_A), to_tumbler=Tumbler.parse(CLEAN_B), link_type="relates",
                        from_span="", to_span="", created_by="t", created_at="", meta={}),
        ]

    def _by_tumbler(self, t):
        return next((e for e in self.entries if str(e.tumbler) == str(t)), None)

    def all_documents(self, limit=0, *, content_type="", offset=0):
        return list(self.entries)

    def find(self, query, *, content_type="", limit=50):
        return list(self.entries)

    def find_all(self, query, *, content_type=""):
        return [e for e in self.entries if e.title == query]

    def resolve(self, t):
        return self._by_tumbler(t)

    def resolve_many(self, tumblers):
        return {t: e for t in tumblers if (e := self._by_tumbler(t)) is not None}

    def by_doc_id(self, t):
        return self._by_tumbler(t)

    def links_from(self, t, *a, **k):
        return [l for l in self.links if str(l.from_tumbler) == str(t)]

    def links_to(self, t, *a, **k):
        return [l for l in self.links if str(l.to_tumbler) == str(t)]

    def resolve_span_text(self, t, span):
        return SECRET_SPAN_TEXT if str(t) == SECRET else "ordinary span text"

    def get_owner_by_prefix(self, prefix):
        return None


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    fake = _FakeCatalog()
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: fake)

    def run(*args: str):
        result = CliRunner().invoke(main, ["catalog", *args])
        assert result.exit_code == 0, result.output
        return result.stdout

    run.fake = fake
    return run


def _assert_nothing_leaks(out: str) -> None:
    for secret in (CANARY, SECRET_TITLE, SECRET_PATH, SECRET_URI, SECRET_AUTHOR, SECRET_SPAN_TEXT, SECRET_LINK_NOTE):
        assert secret not in out, f"{secret!r} reached stdout:\n{out}"


def _claude_code(monkeypatch, marker: str = "CLAUDECODE") -> None:
    monkeypatch.setenv(marker, "1")


# ── under Claude Code: the stub, in the row's slot, counted ─────────────────────────────────────────────────


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"])
@pytest.mark.parametrize("verb", ["list", "search"])
def test_claude_code_withholds_a_sensitive_row_in_list_and_search(cli, monkeypatch, marker, verb) -> None:
    _claude_code(monkeypatch, marker)
    out = cli(verb, "q") if verb == "search" else cli(verb)
    lines = out.splitlines()
    assert "Gardening" in out and "Cooking" in out
    assert stub_line(SECRET) in out and withheld_note(1) in out
    assert [i for i, l in enumerate(lines) if CLEAN_A in l or stub_line(SECRET) in l or CLEAN_B in l] == [0, 1, 2], \
        "the stub takes the row's slot between its neighbours"
    _assert_nothing_leaks(out)


@pytest.mark.parametrize("verb", ["list", "search"])
def test_claude_code_json_carries_the_stub_row_not_the_content(cli, monkeypatch, verb) -> None:
    _claude_code(monkeypatch)
    out = cli(verb, "q", "--json") if verb == "search" else cli(verb, "--json")
    _assert_nothing_leaks(out)
    rows = json.loads(out)
    assert [r["tumbler"] for r in rows] == [CLEAN_A, SECRET, CLEAN_B]
    stub = rows[1]
    assert stub["sensitive"] is True and stub["withheld"] == stub_line(SECRET) and "title" not in stub
    assert rows[0]["title"] == "Gardening"


def test_claude_code_show_withholds_the_card_and_every_link_preview(cli, monkeypatch) -> None:
    _claude_code(monkeypatch)
    out = cli("show", SECRET)
    assert stub_line(SECRET) in out and "Collection: knowledge__c__m__v1" in out
    _assert_nothing_leaks(out)


def test_claude_code_show_of_a_clean_document_prints_no_text_of_a_sensitive_link_target(cli, monkeypatch) -> None:
    _claude_code(monkeypatch)
    out = cli("show", CLEAN_A)
    assert "Gardening" in out and f"→ {SECRET} (cites)" in out, "the edge's tumbler and type stay"
    _assert_nothing_leaks(out)


def test_claude_code_show_json_is_the_stub_with_edges_that_lost_their_meta(cli, monkeypatch) -> None:
    _claude_code(monkeypatch)
    out = cli("show", SECRET, "--json")
    _assert_nothing_leaks(out)
    d = json.loads(out)
    assert d["sensitive"] is True and d["withheld"] == stub_line(SECRET) and "title" not in d
    assert [(l["from"], l["to"], l["type"]) for l in d["links_to"]] == [(CLEAN_A, SECRET, "cites")]
    assert d["links_to"][0]["meta"] == {}


# ── in a terminal: the whole row with a badge ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["list", "search"])
def test_a_terminal_prints_the_sensitive_row_with_a_badge_and_the_reader_clause(cli, verb) -> None:
    out = cli(verb, "q") if verb == "search" else cli(verb)
    row = next(l for l in out.splitlines() if SECRET in l)
    assert BADGE in row and SECRET_TITLE in row
    body = out.replace(SENSITIVE_READER_CLAUSE, "")
    assert [l for l in body.splitlines() if BADGE in l] == [row], "only the sensitive row is badged"
    assert SENSITIVE_READER_CLAUSE in out
    assert withheld_note(1) not in out


def test_a_terminal_shows_the_sensitive_card_badged(cli) -> None:
    out = cli("show", SECRET)
    assert BADGE in out and SECRET_TITLE in out and SENSITIVE_READER_CLAUSE in out


def test_a_terminal_json_marks_the_row_and_keeps_its_content(cli) -> None:
    rows = json.loads(cli("list", "--json"))
    assert rows[1]["sensitive"] is True and rows[1]["badge"] == BADGE and rows[1]["title"] == SECRET_TITLE
    assert "badge" not in rows[0]


# ── a page with no sensitive row is untouched under either posture ──────────────────────────────────────────


@pytest.mark.parametrize("claude_code", [False, True])
def test_a_clean_page_prints_the_same_under_either_posture(cli, monkeypatch, claude_code) -> None:
    cli.fake.entries = [e for e in cli.fake.entries if not e.sensitive]
    if claude_code:
        _claude_code(monkeypatch)
    out = cli("list")
    assert "Gardening" in out and "Cooking" in out
    assert BADGE not in out and "withheld" not in out and SENSITIVE_READER_CLAUSE not in out
