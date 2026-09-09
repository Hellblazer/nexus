"""GH #1489 (nexus-zhxxd): ``nx taxonomy discover`` reports a topic persist
conflict per collection and fails the run, instead of printing ``skipped``
and exiting 0 for every collection.

The store's benign-skip used to swallow a 409/23505 whatever the cause; on a
store whose BIGSERIAL sequence sat behind imported ids that hid a defect
behind ``Total: 0 topics``. The store now raises
:class:`TopicPersistConflictError` when the collection has no topics, and
the verb must surface it: one FAILED line naming the collection, the other
collections still discovered, non-zero exit at the end.
"""

from __future__ import annotations

from contextlib import contextmanager

from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy
from nexus.db.t2.http_taxonomy_store import TopicPersistConflictError


def _stub_command_wiring(monkeypatch) -> None:
    class _Taxonomy:
        pass

    @contextmanager
    def _fake_db(*_a, **_k):
        class _Db:
            taxonomy = _Taxonomy()

        yield _Db()

    monkeypatch.setattr("nexus.commands.taxonomy_cmd._T2Database", _fake_db)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._command_shared_t2_client", lambda: None)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._claude_available", lambda: False)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._run_discover_projection", lambda *a, **k: None)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    monkeypatch.setattr("nexus.config.load_config", lambda: {"taxonomy": {"auto_label": False}})
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)


def test_discover_reports_a_persist_conflict_per_collection_and_fails_the_run(monkeypatch) -> None:
    _stub_command_wiring(monkeypatch)
    monkeypatch.setattr(
        "nexus.commands.taxonomy_cmd._enumerate_discoverable_collections",
        lambda t3, exclude: ["rdr__1-3__bge-base-en-v15-768__v1", "docs__1-18__bge-base-en-v15-768__v1"],
    )

    def _discover(col_name, store, t3, *, force=False):
        if col_name.startswith("rdr__"):
            raise TopicPersistConflictError(
                f"persist_discovered for {col_name!r}: the engine answered HTTP 409 "
                "(sqlstate=23505, constraint=topics_pk) and the collection has no topics"
            )
        return 4

    monkeypatch.setattr("nexus.commands.taxonomy_cmd.discover_for_collection", _discover)

    result = CliRunner().invoke(taxonomy, ["discover", "--all"])

    assert result.exit_code != 0, result.output
    assert "rdr__1-3__bge-base-en-v15-768__v1: FAILED: " in result.output
    assert "constraint=topics_pk" in result.output
    assert "rdr__1-3__bge-base-en-v15-768__v1: skipped" not in result.output
    assert "docs__1-18__bge-base-en-v15-768__v1: 4 topics" in result.output
    assert "topic persist failed for 1 collection(s): rdr__1-3__bge-base-en-v15-768__v1" in result.output


def test_discover_still_prints_skipped_for_a_zero_topic_collection(monkeypatch) -> None:
    """The ``skipped`` line keeps its one meaning: discovery persisted nothing
    (no specs, or the existing-topics guard), never a swallowed conflict."""
    _stub_command_wiring(monkeypatch)
    monkeypatch.setattr(
        "nexus.commands.taxonomy_cmd._enumerate_discoverable_collections",
        lambda t3, exclude: ["docs__1-18__bge-base-en-v15-768__v1"],
    )
    monkeypatch.setattr(
        "nexus.commands.taxonomy_cmd.discover_for_collection", lambda *a, **k: 0,
    )

    result = CliRunner().invoke(taxonomy, ["discover", "--all"])

    assert result.exit_code == 0, result.output
    assert "docs__1-18__bge-base-en-v15-768__v1: skipped" in result.output
    assert "FAILED" not in result.output
