# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CLI output verbs that name catalog documents apply the sensitive-output posture (nexus-rfvyy.9 item 2).

Sam 2026-11-08 (ask rfvyy9-cli-posture, option b): the posture RDR-243 Q2 decided for search and the catalog views
extends to the verbs that print a document's title or path in their own lines (catalog delete, reconcile, orphans,
trash, gc, prune-stale, remediate-paths, reconcile-stale, the store_put integrity report, the enrich aspect views);
lookups by exact title stay as they are. Under Claude Code (``nexus.session.started_by_claude_code``) a flagged
document's line is the stub line (its tumbler, no title or path), counted; in a terminal it prints with the
``[sensitive]`` badge and the reader clause.

Tenant-free: a fake catalog stands in for the engine's, because what is under test is how a line is rendered, and
the canary in a flagged document's title and path must not reach stdout when withheld.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry
from nexus.cli import main
from nexus.commands.catalog_cmds import doctor
from nexus.commands.catalog_cmds import reconcile_stale as rs
from nexus.context_annotations import SENSITIVE_READER_CLAUSE
from nexus.sensitive_output import (
    BADGE, CliDocumentLines, entries_flagged, flagged_tumblers, stub_line, withheld_note, withhold_flagged_rows,
)

CANARY = "CANARY-outverbs-7713"
SECRET_TITLE = f"{CANARY} board minutes"
SECRET_PATH = f"/private/{CANARY}/minutes.md"
CLEAN, SECRET, ALIAS = "1.1.1", "1.1.2", "1.1.3"


def _entry(tumbler: str, title: str, *, sensitive: bool = False, path: str = "", miss: int = 0,
           alias_of: str = "") -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title, author="", year=2020, content_type="knowledge",
        file_path=path, corpus="c", physical_collection="knowledge__c__m__v1", chunk_count=3, head_hash="h",
        indexed_at="2026-01-01T00:00:00Z", source_uri=f"file://{path}" if path else "", sensitive=sensitive,
        meta={"miss_count": miss}, alias_of=alias_of,
    )


class _FakeCatalog:
    def __init__(self) -> None:
        self.entries = [
            _entry(CLEAN, "Gardening", path="/pub/garden.md", miss=2),
            _entry(SECRET, SECRET_TITLE, sensitive=True, path=SECRET_PATH, miss=2),
        ]
        # The trash rows carry their flag (nexus-rfvyy.9); the tombstones themselves are invisible to every live read
        # below, as they are to the engine's /show and /resolve_many.
        self.trash = [
            {"tumbler": "1.9.1", "title": "Gardening", "deleted_at": "2026-11-01T00:00:00Z", "sensitive": False},
            {"tumbler": "1.9.2", "title": SECRET_TITLE, "deleted_at": "2026-11-01T00:00:00Z", "sensitive": True},
        ]

    def _by(self, t):
        return next((e for e in self.entries if str(e.tumbler) == str(t)), None)

    def all_documents(self, limit=0, *, content_type="", offset=0):
        return list(self.entries) if offset == 0 else []

    def resolve(self, t, follow_alias=False):
        e = self._by(t)
        if e is not None and follow_alias and e.alias_of:
            return self._by(e.alias_of)
        return e

    def resolve_many(self, tumblers):
        return {t: e for t in tumblers if (e := self._by(t)) is not None}

    def by_doc_id(self, t):
        return self._by(t)

    def find_all(self, query, *, content_type=""):
        return [e for e in self.entries if e.title == query]

    def orphaned_docs(self):
        return [{"tumbler": str(e.tumbler), "title": e.title, "content_type": e.content_type,
                 "file_path": e.file_path} for e in self.entries]

    def list_trash(self, limit=200):
        return list(self.trash)


class _FakeWriter:
    def delete_document(self, t):
        return True


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    fake = _FakeCatalog()
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: fake)
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: _FakeWriter())
    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: fake)

    def run(*args: str, input: str | None = None):
        result = CliRunner().invoke(main, ["catalog", *args], input=input)
        assert result.exit_code == 0, result.output
        return result.stdout

    run.fake = fake
    return run


def _claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")


def _assert_withheld(out: str, n: int = 1) -> None:
    assert CANARY not in out, f"the flagged document's title or path reached stdout:\n{out}"
    assert stub_line(SECRET) in out
    assert f"[{withheld_note(n)}]" in out
    assert "Gardening" in out or "garden" in out, "an unflagged document is printed as before"


def _assert_badged(out: str) -> None:
    assert SECRET_TITLE in out or SECRET_PATH in out
    assert BADGE in out
    assert SECRET_READER in out


SECRET_READER = SENSITIVE_READER_CLAUSE


# ── the verbs, under Claude Code and in a terminal ─────────────────────────────────────────────────────────


def test_orphans_withholds_under_claude_code_and_badges_in_a_terminal(cli, monkeypatch) -> None:
    _assert_badged(cli("orphans", "--no-links"))
    _claude_code(monkeypatch)
    _assert_withheld(cli("orphans", "--no-links"))


def test_trash_withholds_a_tombstoned_flagged_title(cli, monkeypatch) -> None:
    """The flag comes from the trash row: no live read returns a tombstone (the fake's resolve_many and by_doc_id
    do not know these tumblers, as the engine's do not)."""
    assert cli.fake.resolve_many(["1.9.1", "1.9.2"]) == {}
    out = cli("trash")
    assert SECRET_TITLE in out and BADGE in out and SECRET_READER in out
    _claude_code(monkeypatch)
    out = cli("trash")
    assert CANARY not in out and stub_line("1.9.2") in out and "Gardening" in out
    assert f"[{withheld_note(1)}]" in out


def test_trash_follows_a_tombstoned_alias_to_its_flagged_canonical(cli, monkeypatch) -> None:
    """A merged duplicate's own flag is unset; a canonical flagged after the merge still withholds the alias."""
    cli.fake.trash.append({"tumbler": "1.9.3", "title": f"{CANARY} alias copy", "deleted_at": "2026-11-01T00:00:00Z",
                           "sensitive": False, "alias_of": SECRET})
    _claude_code(monkeypatch)
    out = cli("trash")
    assert CANARY not in out and stub_line("1.9.3") in out
    assert f"[{withheld_note(2)}]" in out


def test_trash_from_an_engine_without_the_flag_withholds_every_row_under_claude_code(cli, monkeypatch) -> None:
    for row in cli.fake.trash:
        del row["sensitive"]
    assert "Gardening" in cli("trash"), "in a terminal an unknown flag prints plainly"
    _claude_code(monkeypatch)
    out = cli("trash")
    assert CANARY not in out and "Gardening" not in out
    assert f"[{withheld_note(2)}]" in out


def test_gc_withholds_a_flagged_orphan(cli, monkeypatch) -> None:
    _assert_badged(cli("gc"))
    _claude_code(monkeypatch)
    _assert_withheld(cli("gc"))


def test_delete_names_a_flagged_document_by_its_stub_in_the_prompt_and_the_result(cli, monkeypatch) -> None:
    _claude_code(monkeypatch)
    out = cli("delete", SECRET, input="y\n")
    assert CANARY not in out
    assert out.count(stub_line(SECRET)) == 2, out
    assert f"[{withheld_note(1)}]" in out, "one document, counted once"
    clean = cli("delete", CLEAN, input="y\n")
    assert "Gardening" in clean


def test_an_unreadable_catalog_withholds_every_orphan(cli, monkeypatch) -> None:
    """The orphan rows carry no flag, so the flag is read; when that read fails, every row is withheld."""
    _claude_code(monkeypatch)

    def _boom():
        raise RuntimeError("catalog down")

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", _boom)
    monkeypatch.setattr(cli.fake, "resolve_many", lambda ts: (_ for _ in ()).throw(RuntimeError("down")))
    out = cli("orphans", "--no-links")
    assert CANARY not in out and "Gardening" not in out
    assert f"[{withheld_note(2)}]" in out


# ── the pieces ─────────────────────────────────────────────────────────────────────────────────────────────


def test_cli_document_lines_label_and_notes() -> None:
    w = CliDocumentLines("withhold")
    assert w.label("1.2", False, "plain") == "plain"
    assert w.label("1.2", True, SECRET_TITLE) == stub_line("1.2")
    assert w.notes() == [f"[{withheld_note(1)}]"]
    f = CliDocumentLines("flag")
    assert f.label("1.2", True, SECRET_TITLE) == f"{SECRET_TITLE}  {BADGE}"
    assert f.notes() == [SENSITIVE_READER_CLAUSE]
    assert CliDocumentLines("withhold").notes() == []


def test_flagged_tumblers_fails_closed(monkeypatch) -> None:
    class _Down:
        def resolve_many(self, ts):
            raise RuntimeError("down")

    assert flagged_tumblers(["1.1", "1.2", ""], cat=_Down()) == {"1.1", "1.2"}
    assert flagged_tumblers([], cat=_Down()) == set()


def test_flagged_tumblers_counts_a_tumbler_the_live_catalog_does_not_return_as_flagged() -> None:
    """A tombstoned or purged document keeps no flag a live read can see, so its rows are withheld."""
    fake = _FakeCatalog()
    assert flagged_tumblers([CLEAN, SECRET, "1.9.9"], cat=fake) == {SECRET, "1.9.9"}


def test_the_integrity_report_follows_a_merged_alias_to_its_flagged_canonical(monkeypatch) -> None:
    """A merged duplicate's own flag is unset; the report row of an alias of a flagged note is flagged."""
    fake = _FakeCatalog()
    canonical = _entry(SECRET, SECRET_TITLE, sensitive=True)
    canonical.meta["doc_id"] = "c" * 64
    alias = _entry(ALIAS, SECRET_TITLE, alias_of=SECRET)
    alias.meta["doc_id"] = "d" * 64
    fake.entries = [canonical, alias]
    fake.get_manifest = lambda t: []  # chunk_count 3 against 0 manifest rows: both drift
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: fake)
    monkeypatch.setattr("nexus.db.make_t3", lambda: (_ for _ in ()).throw(RuntimeError("no T3 here")))
    report = doctor._run_store_put_integrity()
    rows = {r["tumbler"]: r for r in report["drift"]}
    assert rows[SECRET].get("sensitive") is True
    assert rows[ALIAS].get("sensitive") is True, "the alias follows its flagged canonical"


def test_entries_flagged_follows_an_alias_to_its_flagged_canonical() -> None:
    fake = _FakeCatalog()
    alias = _entry(ALIAS, SECRET_TITLE, path=SECRET_PATH, alias_of=SECRET)
    fake.entries.append(alias)
    assert entries_flagged(fake.entries, cat=fake) == {SECRET, ALIAS}


def test_withhold_flagged_rows_reaches_nested_rows_and_keeps_only_named_keys() -> None:
    payload = {"drift": [{"tumbler": SECRET, "title": SECRET_TITLE, "sensitive": True, "chunk_count": 3},
                         {"tumbler": CLEAN, "title": "Gardening", "sensitive": False}]}
    w = CliDocumentLines("withhold")
    out = withhold_flagged_rows(payload, w, keep=("chunk_count",))
    assert CANARY not in json.dumps(out)
    assert out["drift"][0] == {"tumbler": SECRET, "chunk_count": 3, "sensitive": True, "withheld": stub_line(SECRET)}
    assert out["drift"][1]["title"] == "Gardening"
    assert w.withheld == 1
    f = withhold_flagged_rows(payload, CliDocumentLines("flag"))
    assert f["drift"][0]["title"] == SECRET_TITLE and f["drift"][0]["badge"] == BADGE


def test_reconcile_stale_samples_withhold_a_flagged_row(monkeypatch, capsys) -> None:
    _claude_code(monkeypatch)
    monkeypatch.setattr(rs, "_DOC_LINES", CliDocumentLines())
    rows = [
        {"tumbler": SECRET, "physical_collection": "c", "title": SECRET_TITLE, "chunk_count": 0, "sensitive": True},
        {"tumbler": CLEAN, "physical_collection": "c", "title": "Gardening", "chunk_count": 0, "sensitive": False},
    ]
    rs._echo_sample(rows, 10, rs._label_zero_count)
    out = capsys.readouterr().out
    assert CANARY not in out and stub_line(SECRET) in out and "Gardening" in out
    assert rs._DOC_LINES.withheld == 1


def test_store_put_integrity_text_withholds_flagged_titles(monkeypatch, capsys) -> None:
    report = {
        "pass": False, "checked": 2,
        "drift": [{"tumbler": SECRET, "title": SECRET_TITLE, "sensitive": True, "chunk_count": 1, "manifest_count": 0}],
        "ghosts": [{"tumbler": SECRET, "title": SECRET_TITLE, "sensitive": True, "verified_by": "chash"},
                   {"tumbler": CLEAN, "title": "Gardening", "sensitive": False, "verified_by": "chash"}],
        "unverifiable": [{"tumbler": SECRET, "title": SECRET_TITLE, "sensitive": True, "reason": "T3 down"}],
        "check_errors": [],
    }
    lines = CliDocumentLines("withhold")
    doctor._print_store_put_integrity_text(report, lines)
    out = capsys.readouterr().out
    assert CANARY not in out and out.count(stub_line(SECRET)) == 3 and "Gardening" in out
    assert lines.withheld == 3
