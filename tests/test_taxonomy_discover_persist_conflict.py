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
import pytest


def _stub_command_wiring(monkeypatch) -> None:
    class _Taxonomy:
        pass

    @contextmanager
    def _fake_db(*_a, **_k):
        class _Db:
            taxonomy = _Taxonomy()
            memory = object()  # the discover-health record's store; the tests capture the recorder itself

        yield _Db()

    monkeypatch.setattr("nexus.commands.taxonomy_cmd._T2Database", _fake_db)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._command_shared_t2_client", lambda: None)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._claude_available", lambda: False)
    monkeypatch.setattr("nexus.commands.taxonomy_cmd._run_discover_projection", lambda *a, **k: None)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    monkeypatch.setattr("nexus.config.load_config", lambda: {"taxonomy": {"auto_label": False}})
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)


@pytest.mark.t2_tenant
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
    # nexus-s08oe: every attempt is recorded in the discover-health store, as `nx index` records it.
    calls: list[tuple] = []
    monkeypatch.setattr(
        "nexus.mcp_infra.record_taxonomy_discover_attempt",
        lambda memory, collection, *, success, error_class="": calls.append((collection, success, error_class)),
    )

    result = CliRunner().invoke(taxonomy, ["discover", "--all"])

    assert result.exit_code != 0, result.output
    assert calls == [
        ("rdr__1-3__bge-base-en-v15-768__v1", False, "TopicPersistConflictError"),
        ("docs__1-18__bge-base-en-v15-768__v1", True, ""),
    ]
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

    calls: list[tuple] = []
    monkeypatch.setattr(
        "nexus.mcp_infra.record_taxonomy_discover_attempt",
        lambda memory, collection, *, success, error_class="": calls.append((collection, success, error_class)),
    )

    result = CliRunner().invoke(taxonomy, ["discover", "--all"])

    assert result.exit_code == 0, result.output
    assert "docs__1-18__bge-base-en-v15-768__v1: skipped" in result.output
    # A run that formed no topics still ran: recorded as a success (the doctor's re-embed row clears on it).
    assert calls == [("docs__1-18__bge-base-en-v15-768__v1", True, "")]
    assert "FAILED" not in result.output



def test_the_attempt_record_is_best_effort_even_without_a_memory_store() -> None:
    """A db with no ``memory`` attribute (a fixture stand-in) must not change the discover's outcome."""
    from nexus.commands.taxonomy_cmd import _record_discover_attempt

    _record_discover_attempt(object(), "docs__1-1__a__v1", success=True)
