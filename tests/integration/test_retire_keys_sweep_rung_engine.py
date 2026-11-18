# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.43: the ``retire-keys-sweep`` rung against the real engine, with the production client.

One journey (it mints one tenant, so it lives with the integration tests the tenant-count ceiling does not count): chunk rows carrying retired keys, a manifest row whose NULL span the engine counts as
a precondition that stands in the way, then the operator-free path the rung takes. The rung under test is built with its
defaults except for the completion record and the managed switch, so ``make_t3`` and the engine's own dry run and apply
job are the ones a local install runs. The unit twin, which scripts the engine's answers, is
``test_retire_keys_sweep_rung.py``.
"""
from __future__ import annotations

import pytest

from nexus.upgrade_ladder.protocol import ConvergeOutcome
from nexus.upgrade_ladder.retire_keys_state import default_state_store
from nexus.upgrade_ladder.rungs.retire_keys_sweep import RetireKeysSweepRung
from tests.test_retire_keys import _last_written, _metadata
from tests.upgrade._substrate_sql import MODEL, seed_chunk

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_SPAN = {"line_start": 3, "line_end": 9, "chunk_start_char": 40, "chunk_end_char": 190}
_RETIRED = ("title", "tags", "doc_id", "chunk_index")


class Reporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class Spy:
    """The production T3 client, with a record of the calls the rung makes through it."""

    def __init__(self, client) -> None:
        self._client = client
        self.calls: list[str] = []

    def __getattr__(self, name):
        target = getattr(self._client, name)
        if not callable(target):
            return target

        def call(*a, **kw):
            self.calls.append(name)
            return target(*a, **kw)

        return call


@pytest.fixture
def catalog(t2_service_env):
    from tests._catalog_fixture_ops import ActiveCatalog  # noqa: PLC0415

    cat = ActiveCatalog()
    return cat, cat.register_owner("retire43", "curator")


def _rung(spy_holder: list[Spy] | None = None) -> RetireKeysSweepRung:
    from nexus.db import make_t3  # noqa: PLC0415

    def client() -> Spy:
        spy = Spy(make_t3())
        if spy_holder is not None:
            spy_holder.append(spy)
        return spy

    return RetireKeysSweepRung(client_factory=client, record_fn=lambda: None, managed_fn=lambda: False,
                               state_store=default_state_store())


def test_the_rung_sweeps_a_seeded_install_defers_on_a_standing_precondition_and_a_rerun_is_a_no_op(
    t2_service_env, catalog,
) -> None:
    tenant = t2_service_env
    cat, owner = catalog
    free = f"knowledge__retire43free__{MODEL}__v1"
    held = f"knowledge__retire43held__{MODEL}__v1"
    stamp = {"indexed_at": "2026-01-01T00:00:00+00:00"}
    ownerless = [
        seed_chunk(tenant, free, f"ownerless {i} retire43", {"title": f"t{i}", "tags": "a,b", "doc_id": "nodoc",
                                                                "chunk_index": i})
        for i in range(3)
    ]
    c_held = seed_chunk(tenant, held, "owned retire43", {**_SPAN, **stamp, "title": "held"})
    # An indexed document carries the generated meta keys the engine counts (backfill-meta's residue otherwise).
    doc = str(cat.register(owner, "held", content_type="paper", physical_collection=held, chunk_count=1,
                           meta={"source_format": "pdf", "tags": ["x"], "category": "paper"}))
    cat.write_manifest(doc, [{"chash": c_held, "position": 0}], collection=held)
    rows = [(free, h) for h in ownerless] + [(held, c_held)]
    stamps = [_last_written(tenant, c, h) for c, h in rows]

    def retired_keys_left() -> int:
        return sum(1 for c, h in rows for k in (*_RETIRED, *_SPAN) if k in _metadata(tenant, c, h))

    # Non-vacuity: every row holds retired keys, and the manifest row has NULL spans while its chunk row holds them.
    assert all(_metadata(tenant, c, h)["title"] for c, h in rows)
    (manifest_row,) = cat.get_manifest(doc)
    assert (manifest_row.line_start, manifest_row.char_end) == (None, None)
    before = retired_keys_left()
    assert before == 3 * 4 + 5, "four retired keys on each ownerless row, a title and the four span keys on the owned one"

    # detect is the dry run: it names the rows and what stands in the way, and writes nothing.
    r = _rung()
    status = r.detect()
    assert status.applicable and not status.converged
    assert "4 chunk row(s)" in status.pending_detail and "null_span_rows=1" in status.pending_detail, status.pending_detail

    # converge: the NULL span stands in the way. DEFERRED, naming the count and the operator command, and not one row changes.
    spies: list[Spy] = []
    r = _rung(spies)
    result = r.converge(Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED, result
    assert "null_span_rows=1" in result.detail and "--accept null_span_rows=1" in result.detail, result.detail
    assert [c for s in spies for c in s.calls] == ["chunk_keys_dry_run"], "no apply was started"
    assert retired_keys_left() == before, "a deferral writes nothing"
    measured = default_state_store().load()
    assert measured is not None and measured["outcome"] == "blocked" and measured["blocking"] == {"null_span_rows": 1}
    assert "accept it with" in measured["detail"], "the full text is kept for `nx doctor`'s row"
    assert "still held by null_span_rows=1" in _rung().converge(Reporter()).detail, "the next run is one line"
    assert [_last_written(tenant, c, h) for c, h in rows] == stamps

    # The operator-free fix: the manifest row takes the span the chunk row holds (what the span-fill rung does). The
    # sweep then has nothing standing in the way.
    cat.write_manifest(doc, [{
        "chash": c_held, "position": 0, "line_start": 3, "line_end": 9, "char_start": 40, "char_end": 190,
    }], collection=held)
    (filled,) = cat.get_manifest(doc)
    assert (filled.line_start, filled.line_end, filled.char_start, filled.char_end) == (3, 9, 40, 190)

    spies = []
    r = _rung(spies)
    reporter = Reporter()
    assert r.detect().pending
    result = r.converge(reporter)
    while result.outcome is ConvergeOutcome.RESUMABLE:
        result = r.converge(reporter)
    assert result.outcome is ConvergeOutcome.COMPLETED, result
    assert r.verify() is True and "dry run counts no retired-key row" in r.record_detail()
    for c, h in rows:
        assert set(_metadata(tenant, c, h)) == {"chunk_text_hash"}, (c, h)
    assert retired_keys_left() == 0
    assert [_last_written(tenant, c, h) for c, h in rows] == stamps, "last_written_at is the reaper's grace anchor"
    (kept,) = cat.get_manifest(doc)
    assert (kept.line_start, kept.line_end, kept.char_start, kept.char_end) == (3, 9, 40, 190), "the catalog is untouched"
    assert any(e == "retire_keys_sweep_batch" and f["swept"] for e, f in reporter.events), reporter.events
    swept_note = default_state_store().load()
    assert swept_note is not None and swept_note["outcome"] == "swept" and swept_note["retired_key_rows"] == 0

    # A rerun on the converged install is a no-op: detect reads converged, converge starts no apply, verify holds.
    spies = []
    again = _rung(spies)
    assert again.detect().converged
    assert again.converge(Reporter()).completed
    assert again.verify() is True
    assert "chunk_keys_apply" not in [c for s in spies for c in s.calls], "nothing to sweep, so no apply job"
    assert retired_keys_left() == 0
