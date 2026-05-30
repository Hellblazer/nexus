# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1.7 contracts for DEVONthink Layer F write-back (RDR-139).

Pins (fake DT client injected — no live DB):
- --writeback present → nx-indexed / nx-tumbler / nx-kw tags stamped (add-mode),
  annotation appended, custom metadata merged; the exact tag set is asserted.
- nexus-owned namespace: every written tag is nx-* prefixed.
- no-clobber modes (add/append/merge) are the modes actually passed.
- idempotent: a second call passes the same args (DT add/merge/append dedup).
- DT unavailable → skipped, zero writes (Gap 0).
- excluded record (a write helper returns False) → that flag False, others
  still attempt; never raises (clean error, not silent partial corruption).
"""

from __future__ import annotations

from nexus.dt_writeback import writeback_record


class _FakeDT:
    def __init__(self, *, available=True, tag_ok=True, ann_ok=True, meta_ok=True):
        self._available = available
        self._tag_ok = tag_ok
        self._ann_ok = ann_ok
        self._meta_ok = meta_ok
        self.calls: list[tuple[str, tuple, dict]] = []

    def available(self, *, refresh=False):
        return self._available

    def dt_set_tags(self, uuid, tags, *, mode="add"):
        self.calls.append(("tags", (uuid, tuple(tags)), {"mode": mode}))
        return self._tag_ok

    def dt_set_annotation(self, uuid, text, *, mode="append"):
        self.calls.append(("annotation", (uuid, text), {"mode": mode}))
        return self._ann_ok

    def dt_set_custom_metadata(self, uuid, fields, *, mode="merge"):
        self.calls.append(("metadata", (uuid, tuple(sorted(fields.items()))), {"mode": mode}))
        return self._meta_ok


def test_writeback_stamps_all_three_with_no_clobber_modes():
    dt = _FakeDT()
    out = writeback_record("U", "1.2.3", aspect_keywords=["TPC-C", "tpc-c", "RAG"], dt_client=dt)
    assert out == {"tags": True, "annotation": True, "metadata": True, "skipped": False}

    by_kind = {c[0]: c for c in dt.calls}
    # Tag set: nx-indexed, nx-tumbler, deduped+lowercased nx-kw.
    _, (uuid, tags), kw = by_kind["tags"]
    assert uuid == "U"
    assert kw["mode"] == "add"
    assert set(tags) == {"nx-indexed", "nx-tumbler:1.2.3", "nx-kw:tpc-c", "nx-kw:rag"}
    # Every tag is nexus-owned.
    assert all(t.startswith("nx-") for t in tags)
    # Annotation appended, metadata merged.
    assert by_kind["annotation"][2]["mode"] == "append"
    assert "1.2.3" in by_kind["annotation"][1][1]
    assert by_kind["metadata"][2]["mode"] == "merge"


def test_writeback_unavailable_makes_no_writes():
    dt = _FakeDT(available=False)
    out = writeback_record("U", "1.2.3", dt_client=dt)
    assert out == {"tags": False, "annotation": False, "metadata": False, "skipped": True}
    assert dt.calls == []


def test_writeback_idempotent_args_on_rerun():
    dt = _FakeDT()
    writeback_record("U", "1.2.3", dt_client=dt)
    first = list(dt.calls)
    dt.calls.clear()
    writeback_record("U", "1.2.3", dt_client=dt)
    assert dt.calls == first  # same add/append/merge calls — DT dedups server-side


def test_writeback_excluded_record_clean_partial_not_crash():
    # An excluded record: the DT server rejects the tag write (helper returns
    # False). The other writes still attempt; nothing raises.
    dt = _FakeDT(tag_ok=False)
    out = writeback_record("U", "1.2.3", dt_client=dt)
    assert out["tags"] is False
    assert out["skipped"] is False
    assert {c[0] for c in dt.calls} == {"tags", "annotation", "metadata"}


def test_writeback_empty_identity_skips():
    dt = _FakeDT()
    assert writeback_record("", "1.2.3", dt_client=dt)["skipped"] is True
    assert writeback_record("U", "", dt_client=dt)["skipped"] is True
    assert dt.calls == []
