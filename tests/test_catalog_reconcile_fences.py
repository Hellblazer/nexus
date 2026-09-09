"""GH #1512 (nexus-kt7f4): ``nx catalog reconcile-fences`` stamps index-run
fences on non-repo documents that carry none, through the same begin +
verify-then-stamp pair the fenced write paths use; repo documents are left
to ``nx index <path> --force``; a document the engine refuses (manifest not
whole in T3) is named, never stamped."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from nexus.commands.catalog_cmds.reconcile_fences import (
    _content_hash_for,
    reconcile_fences,
    reconcile_fences_cmd,
)
from nexus.errors import IndexRunVerifyRefused


def _entry(tumbler: str, coll: str, *, state=None, reported=True):
    return SimpleNamespace(tumbler=tumbler, physical_collection=coll, index_state=state,
                           index_state_reported=reported)


class _Reader:
    def __init__(self, entries, manifests):
        self._entries, self._manifests = entries, manifests

    def list_owners(self, *, include_deactivated=False):
        return [{"tumbler_prefix": "1.1", "owner_type": "curator"},
                {"tumbler_prefix": "1.61", "owner_type": "repo"}]

    def all_documents(self, limit=0):
        return list(self._entries)

    def get_manifest(self, doc_id):
        return [SimpleNamespace(chash=c) for c in self._manifests.get(doc_id, [])]


class _Writer:
    def __init__(self, refuse: set[str] = frozenset()):
        self.begun: list[tuple] = []
        self.completed: list[tuple] = []
        self._refuse = refuse

    def begin_index_run(self, doc_id, content_hash, run_id, collection):
        self.begun.append((doc_id, content_hash, collection))

    def complete_index_run(self, doc_id, content_hash, chunk_count):
        if doc_id in self._refuse:
            raise IndexRunVerifyRefused(doc_id=doc_id, referenced=chunk_count, present=chunk_count - 1,
                                        missing=1, chunk_count=chunk_count)
        self.completed.append((doc_id, content_hash, chunk_count))

    def close(self):
        pass


def test_content_hash_is_the_single_chash_or_the_digest_over_the_manifest() -> None:
    assert _content_hash_for([SimpleNamespace(chash="a" * 64)]) == "a" * 64
    two = _content_hash_for([SimpleNamespace(chash="a" * 64), SimpleNamespace(chash="b" * 64)])
    assert len(two) == 64 and two != "a" * 64


def test_stamps_whole_non_repo_documents_and_skips_the_rest() -> None:
    entries = [
        _entry("1.1.103", "knowledge__knowledge__bge-base-en-v15-768__v1"),   # curator, whole -> stamped
        _entry("1.1.104", "knowledge__knowledge__bge-base-en-v15-768__v1"),   # curator, refused by the engine
        _entry("1.1.105", "knowledge__knowledge__bge-base-en-v15-768__v1"),   # curator, empty manifest
        _entry("1.1.106", "knowledge__knowledge__bge-base-en-v15-768__v1", state="complete"),  # already stamped
        _entry("1.61.7", "code__1-61__bge-base-en-v15-768__v1"),              # repo: not this command's
        _entry("1.1.107", "knowledge__x", reported=False),                    # pre-fence engine: unknown
    ]
    manifests = {"1.1.103": ["a" * 64], "1.1.104": ["b" * 64, "c" * 64]}
    reader, writer = _Reader(entries, manifests), _Writer(refuse={"1.1.104"})
    lines: list[str] = []

    counts = reconcile_fences(reader, writer, dry_run=False, limit=0, echo=lines.append)

    assert counts == {"candidates": 3, "stamped": 1, "refused": 1, "empty_manifest": 1, "skipped_repo": 1}
    assert writer.completed == [("1.1.103", "a" * 64, 1)]
    assert [b[0] for b in writer.begun] == ["1.1.103", "1.1.104"]
    assert any("refused 1.1.104" in ln for ln in lines)
    assert lines[-1].startswith("stamped 1 of 3 non-repo document(s)")


def test_dry_run_writes_nothing_and_names_the_documents() -> None:
    reader = _Reader([_entry("1.1.103", "knowledge__k")], {"1.1.103": ["a" * 64]})
    writer = _Writer()
    lines: list[str] = []
    counts = reconcile_fences(reader, writer, dry_run=True, limit=0, echo=lines.append)
    assert counts["stamped"] == 1 and writer.begun == [] and writer.completed == []
    assert lines[0] == "  would stamp 1.1.103 (knowledge__k, 1 chunk(s))"


def test_command_exits_non_zero_when_the_engine_refused_a_document(monkeypatch) -> None:
    reader = _Reader([_entry("1.1.104", "knowledge__k")], {"1.1.104": ["b" * 64]})
    writer = _Writer(refuse={"1.1.104"})
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **kw: reader)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda **kw: writer)
    result = CliRunner().invoke(reconcile_fences_cmd, [])
    assert result.exit_code != 0
    assert "1 document(s) refused" in result.output
