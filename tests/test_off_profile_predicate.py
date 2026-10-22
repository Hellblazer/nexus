# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-n6mh0 (RDR-210 Phase 6 Step 5, S-R2-5): ONE off-profile predicate for the hint, the doctor row and ``--all``.

The RDR: a superseded row is not a candidate, with one exception, a source a ``kept_tombstoned`` job left
superseded once a document is live in it again. A ``kept_by_flag`` or ``kept_residue`` source is never listed.
So after ``--keep-source`` the hint and the doctor row clear; a restored live document in a kept_tombstoned
source keeps them lit, because the routing listing leaves a superseded source out and nothing else serves it.

Model tokens are inert (``voyage-model-a``); the code under test reads only a token's prefix.
"""
from __future__ import annotations

import pytest

from nexus import reembed_job as rj

BGE = "bge-base-en-v15-768"
VA = "voyage-model-a"
PROFILE = {"docs": VA}


def _row(name, *, model=BGE, state="live", superseded_by="", ct="docs"):
    return {
        "name": name, "content_type": ct, "owner_id": "1.1", "embedding_model": model,
        "lifecycle_state": state, "superseded_by": superseded_by,
    }


def _job(source, state="done", disposition=None, target="docs__t"):
    return {
        "job_id": f"j-{source}-{state}", "source_collection": source, "target_collection": target,
        "state": state, "disposition": disposition,
    }


class _Reader:
    """A catalog reader with the reads the predicate makes."""

    def __init__(self, rows, *, live=None, all_=None):
        self._rows = rows
        self._live = live or {}
        self._all = all_ if all_ is not None else dict(self._live)
        self.count_reads = 0

    def list_collections(self):
        return list(self._rows)

    def embedding_profile(self):
        return [{"content_type": ct, "embedding_model": m, "dimension": 3} for ct, m in PROFILE.items()]

    def collection_doc_counts(self, *, include_deleted=False):
        self.count_reads += 1
        return dict(self._all if include_deleted else self._live)


def _source(**kw):
    return _row("docs__s", superseded_by="docs__t", **kw)


def _target():
    return _row("docs__t", model=VA)


KT = "kept_tombstoned"

#: (id, rows, jobs newest first, live docs in the source, all docs in the source, source is off-profile)
SCENARIOS = [
    ("unsuperseded stale row", [_row("docs__s")], [], 0, 0, True),
    ("on profile", [_row("docs__s", model=VA)], [], 0, 0, False),
    ("kept_by_flag", [_source(), _target()], [_job("docs__s", disposition="kept_by_flag")], 0, 0, False),
    ("kept_residue", [_source(), _target()], [_job("docs__s", disposition="kept_residue")], 0, 0, False),
    ("kept_tombstoned, tombstones still there", [_source(), _target()], [_job("docs__s", disposition=KT)], 0, 3, False),
    ("kept_tombstoned, purged (cutover re-entry)", [_source(), _target()], [_job("docs__s", disposition=KT)], 0, 0, False),
    ("kept_tombstoned, a document restored (running re-entry)", [_source(), _target()],
     [_job("docs__s", disposition=KT)], 1, 4, True),
    ("kept_tombstoned, another job holds the lock", [_source(), _target()],
     [_job("docs__s", state="running"), _job("docs__s", disposition=KT)], 1, 4, False),
    ("superseded with no job (a rename tombstone)", [_source(), _target()], [], 1, 1, False),
    ("kept_tombstoned, superseded by a different target", [_row("docs__s", superseded_by="docs__x"), _target()],
     [_job("docs__s", disposition=KT)], 1, 1, False),
]
_IDS = [s[0] for s in SCENARIOS]


def _names(rows):
    return sorted(str(r["name"]) for r in rows)


@pytest.mark.parametrize("label,rows,jobs,live,alld,off", SCENARIOS, ids=_IDS)
def test_the_predicate_by_disposition(label, rows, jobs, live, alld, off):
    got = rj.off_profile_rows(rows, PROFILE, lambda: jobs, lambda n: live, lambda n: alld)
    assert ("docs__s" in _names(got)) is off, label


@pytest.mark.parametrize("label,rows,jobs,live,alld,off", SCENARIOS, ids=_IDS)
def test_the_hint_the_doctor_call_and_all_agree(label, rows, jobs, live, alld, off):
    counts_live = {"docs__s": live}
    counts_all = {"docs__s": alld}
    reader = _Reader(rows, live=counts_live, all_=counts_all)
    # The hint.
    hint = rj.off_profile_hint(lambda: reader, jobs_reader=lambda: jobs)
    # The doctor's call (it passes the rows and profile it already read).
    doctor = rj.off_profile_collections(reader, rows=reader.list_collections(), profile=PROFILE, jobs=lambda: jobs)
    # ``--all`` lists the stale and held rows plus every re-entry. The doctor and the hint
    # take all of it EXCEPT a cutover re-entry (a purged kept_tombstoned source: only the
    # delete is left, nothing is stranded), the one deliberate difference; RDR-210 lists it
    # for --all only (:856, :1810) and the S-R2-5 rule counts a source "holding a live document".
    found = rj.candidates(rows, PROFILE, jobs, lambda n: counts_live.get(n, 0), lambda n: counts_all.get(n, 0))
    listed = found.stale + [r for r, _ in found.held] + [x.source_row for x in found.reentries]
    cutover = [x.source_row for x in found.reentries if x.kind == "cutover"]
    assert _names(doctor) == sorted(set(_names(listed)) - set(_names(cutover))), label
    if "purged" in label:
        assert _names(cutover) == ["docs__s"], "--all still lists the purged source as a cutover re-entry"
        assert "docs__s" not in _names(doctor), "the doctor and the hint do not"
    assert ("docs__s" in _names(doctor)) is off, label
    assert (hint == rj.REEMBED_HINT) is bool(doctor), label


def test_no_superseded_row_means_no_job_read():
    """The common case pays nothing for the exception."""
    reader = _Reader([_row("docs__a", model=VA)])

    def no_jobs():
        raise AssertionError("jobs were read with no superseded row in the catalog")

    assert rj.off_profile_collections(reader, jobs=no_jobs) == []
    assert reader.count_reads == 0


def test_an_unreadable_job_table_leaves_the_superseded_rows_out_and_keeps_the_stale_ones():
    rows = [_source(), _target(), _row("docs__a")]

    def boom():
        raise RuntimeError("engine down")

    got = rj.off_profile_rows(rows, PROFILE, boom, lambda n: 1, lambda n: 1)
    assert _names(got) == ["docs__a"]


def test_no_profile_is_no_drift():
    assert rj.off_profile_collections(_Reader([_row("docs__a")]), profile={}) == []


def test_a_held_stale_row_counts_once_a_superseded_row_forces_the_job_read():
    """A stale row a running job holds stays off-profile: --all reports it as held, and the
    doctor and the hint keep it (code review: dropping the held rows passed every other test)."""
    rows = [_row("docs__h"), _source(), _target()]
    jobs = [_job("docs__h", state="running"), _job("docs__s", disposition=KT)]
    got = rj.off_profile_rows(rows, PROFILE, lambda: jobs, lambda n: 0, lambda n: 3)
    assert _names(got) == ["docs__h"]
    found = rj.candidates(rows, PROFILE, jobs, lambda n: 0, lambda n: 3)
    assert [r["name"] for r, _ in found.held] == ["docs__h"]
    reader = _Reader(rows, live={"docs__s": 0}, all_={"docs__s": 3})
    assert _names(rj.off_profile_collections(reader, rows=rows, profile=PROFILE, jobs=lambda: jobs)) == ["docs__h"]


def test_unreadable_doc_counts_leave_the_superseded_rows_out_and_keep_the_stale_ones():
    rows = [_source(), _target(), _row("docs__a")]
    jobs = [_job("docs__s", disposition=KT)]

    def boom(name):
        raise RuntimeError("engine down")

    got = rj.off_profile_rows(rows, PROFILE, lambda: jobs, boom, boom)
    assert _names(got) == ["docs__a"]

