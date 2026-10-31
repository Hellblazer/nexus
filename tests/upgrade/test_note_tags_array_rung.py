# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.38 (RDR-236 RF-17): the ``note-tags-array`` rung converts a note's tags from the
comma string older clients stored in its catalog entry's ``meta`` to the JSON array.

Storage-free: a catalog fake with the engine's ``meta`` merge stands in for the reader and the
writer, injected through the rung's factories. The same conversion against the real engine is a
step of ``tests/integration/test_6pfgb5_note_meta_homes.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import RUNG_NOTE_TAGS_ARRAY, RUNG_ORDER, default_registry
from nexus.upgrade_ladder.rungs.note_tags_array import NoteTagsArrayRung

NOTES = "knowledge__art__bge-base-en-v15-768__v1"
OTHER = "knowledge__other__bge-base-en-v15-768__v1"
CODE = "code__repo__bge-base-en-v15-768__v1"


class Report:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class World:
    """Catalog documents by tumbler; ``update`` merges ``meta`` as the engine's jsonb_concat does."""

    def __init__(self) -> None:
        self.docs: dict[str, SimpleNamespace] = {}
        self.updates: list[tuple[str, dict]] = []
        self.listing_stale = False
        self.read_error: Exception | None = None
        self.no_catalog = False
        self._stale_snapshot: dict[str, dict] = {}

    def add(self, tumbler: str, meta: dict, *, collection: str = NOTES, content_type: str = "knowledge",
            file_path: str = "", title: str = "") -> str:
        self.docs[tumbler] = SimpleNamespace(
            tumbler=tumbler, title=title or f"title-{tumbler}", content_type=content_type,
            file_path=file_path, physical_collection=collection, meta=dict(meta))
        return tumbler

    # reader ---------------------------------------------------------------
    def distinct_doc_collections(self) -> list[str]:
        if self.read_error is not None:
            raise self.read_error
        return sorted({d.physical_collection for d in self.docs.values()})

    def list_by_collection(self, collection: str, *, limit: int | None = None) -> list[SimpleNamespace]:
        if self.read_error is not None:
            raise self.read_error
        listed = [d for d in self.docs.values() if d.physical_collection == collection]
        if self.listing_stale:  # a snapshot taken before a concurrent put landed
            return [SimpleNamespace(**{**vars(d), "meta": dict(self._stale_snapshot.get(d.tumbler, d.meta))})
                    for d in listed]
        return listed

    def resolve(self, tumbler: str, *, follow_alias: bool = True) -> SimpleNamespace | None:
        return self.docs.get(str(tumbler))

    # writer ---------------------------------------------------------------
    def update(self, tumbler, *, meta=None, **fields) -> None:
        assert not fields, fields
        self.updates.append((str(tumbler), dict(meta or {})))
        doc = self.docs[str(tumbler)]
        doc.meta = {**doc.meta, **(meta or {})}

    def close(self) -> None:
        pass

    def rung(self, **kw) -> NoteTagsArrayRung:
        return NoteTagsArrayRung(
            reader_factory=lambda: None if self.no_catalog else self,
            writer_factory=lambda: self,
            **kw,
        )


@pytest.fixture
def world() -> World:
    return World()


def _tags(world: World) -> dict[str, object]:
    return {t: d.meta.get("tags", "<absent>") for t, d in world.docs.items()}


# ── the rung's shape ─────────────────────────────────────────────────────────


def test_the_rung_is_registered_and_conforms() -> None:
    assert RUNG_NOTE_TAGS_ARRAY == "note-tags-array"
    assert RUNG_NOTE_TAGS_ARRAY in RUNG_ORDER
    assert RUNG_NOTE_TAGS_ARRAY in [r.name for r in default_registry()]
    assert isinstance(NoteTagsArrayRung(), Rung)


# ── detect / converge / verify, one journey ──────────────────────────────────


def test_a_legacy_string_becomes_an_array_and_only_the_tags_key_is_written(world: World) -> None:
    world.add("1.1.1", {"tags": "arch, decision,arch", "category": "design", "doc_id": "x"})
    world.add("1.1.2", {"tags": ""})
    world.add("1.1.3", {"tags": ["already", "array"]})
    world.add("1.1.4", {"category": "no-tags-key"})
    world.add("1.1.5", {"tags": "other-collection"}, collection=OTHER)
    world.add("1.1.6", {"tags": "an,indexed,file"}, collection=CODE, content_type="code", file_path="/r/a.py")
    world.add("1.1.7", {"tags": "pdf-in-a-knowledge-collection"}, content_type="paper", file_path="/r/p.pdf")
    rung = world.rung()

    status = rung.detect()
    assert (status.applicable, status.converged) == (True, False)
    assert "3 note" in status.pending_detail, "1.1.1, 1.1.2 and 1.1.5 hold a string"
    assert world.updates == [], "detect is read-only"

    outcome = rung.converge(Report())
    assert outcome.outcome is ConvergeOutcome.COMPLETED
    assert _tags(world) == {
        "1.1.1": ["arch", "decision"], "1.1.2": [], "1.1.3": ["already", "array"], "1.1.4": "<absent>",
        "1.1.5": ["other-collection"],
        "1.1.6": "an,indexed,file", "1.1.7": "pdf-in-a-knowledge-collection",
    }
    assert world.docs["1.1.1"].meta["category"] == "design" and world.docs["1.1.1"].meta["doc_id"] == "x"
    assert all(set(meta) == {"tags"} for _t, meta in world.updates), "merge: only the tags key is sent"
    assert len(world.updates) == 3

    assert rung.verify() is True
    assert rung.detect().converged is True


def test_a_second_run_changes_nothing(world: World) -> None:
    world.add("1.1.1", {"tags": "a,b"})
    rung = world.rung()
    rung.converge(Report())
    world.updates.clear()

    again = world.rung()
    assert again.detect().converged is True
    assert again.converge(Report()).outcome is ConvergeOutcome.COMPLETED
    assert world.updates == [] and _tags(world) == {"1.1.1": ["a", "b"]}


def test_the_conversion_overwrites_a_string_it_is_exempt_from_the_fill_only_rule(world: World) -> None:
    """The .10 backfill never overwrites a key the entry holds; this one-time conversion rewrites the
    stored string by design, and the array carries the same tags."""
    world.add("1.1.1", {"tags": "kept,tag"})
    world.rung().converge(Report())
    assert world.docs["1.1.1"].meta["tags"] == ["kept", "tag"]


def test_batches_are_resumable(world: World) -> None:
    for i in range(1, 6):
        world.add(f"1.1.{i}", {"tags": f"t{i}"})
    rung = world.rung(batch_limit=2)
    outcomes = []
    for _ in range(6):
        outcomes.append(rung.converge(Report()).outcome)
        if outcomes[-1] is ConvergeOutcome.COMPLETED:
            break
    assert outcomes == [ConvergeOutcome.RESUMABLE, ConvergeOutcome.RESUMABLE, ConvergeOutcome.COMPLETED]
    assert _tags(world) == {f"1.1.{i}": [f"t{i}"] for i in range(1, 6)}


def test_a_note_re_put_by_a_new_client_after_the_listing_is_not_overwritten(world: World) -> None:
    """The listing is a snapshot: a put that landed an array since must win over the stale string."""
    world.add("1.1.1", {"tags": "old,string"})
    world._stale_snapshot = {"1.1.1": {"tags": "old,string"}}
    world.listing_stale = True
    world.docs["1.1.1"].meta["tags"] = ["fresh", "put"]
    world.rung().converge(Report())
    assert world.docs["1.1.1"].meta["tags"] == ["fresh", "put"]
    assert world.updates == []


# ── applicability and verify: positive signal only ───────────────────────────


def test_no_catalog_is_not_applicable(world: World) -> None:
    world.no_catalog = True
    status = world.rung().detect()
    assert (status.applicable, status.converged) == (False, False)


def test_verify_is_false_when_the_read_fails_or_a_string_remains(world: World) -> None:
    world.add("1.1.1", {"tags": "a"})
    rung = world.rung()
    assert rung.verify() is False and "1 note" in rung.verify_detail()

    world.read_error = RuntimeError("catalog 503")
    unreadable = world.rung()
    assert unreadable.verify() is False and "catalog 503" in unreadable.verify_detail()

    world.read_error = None
    world.no_catalog = True
    gone = world.rung()
    assert gone.verify() is False and gone.verify_detail()


def test_verify_counts_what_it_read(world: World) -> None:
    """A pass over nothing it examined is visible: the record detail names the notes it looked at."""
    world.add("1.1.1", {"tags": ["x"]})
    world.add("1.1.2", {})
    world.add("1.1.3", {"tags": "y"})
    rung = world.rung()
    rung.converge(Report())
    assert rung.verify() is True
    assert "3 note" in rung.record_detail() and "1 converted" in rung.record_detail()
