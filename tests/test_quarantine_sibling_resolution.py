# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client's quarantine expiry and re-reference look under more than the sibling the catalog row names today
(nexus-wbfpw.58, RDR-192 Phase 3 critique S3; T2 ``nexus/review-wbfpw55-56-critique``).

The client names the sibling it MOVES into from the origin's catalog row (``quarantine_collection_name``).
The row is mutable: catalog-044-3 rewrote ``owner_id`` on repo collections after chunks had been moved, so the
name derived today is not the name a chunk was moved under yesterday. A client-moved chunk carries no
``quarantined_by`` tag, so the engine's own expiry (vectors-026) skips it, and the client is the only expirer.

Two callers, two reaches (nexus-wbfpw.58 round 2):

* ``nx index repo`` (``indexer._prune_collection_serverside``) runs on every index and uses the TWO derived
  names only: the row-derived name and ``quarantine-<origin name>`` (the reaper's name). It sends no engine
  probe, because the probe is an unindexed scan engine-side. A chunk stranded under a third name stays until
  ``nx t3 gc`` runs.
* ``nx t3 gc`` (``t3._expire_client_quarantine``) also asks the engine which siblings hold the origin's chunks
  (``resolve_quarantine_siblings(..., probe_engine=True)``) and expires from each, reporting each on its own line.

nexus-wbfpw.64 moves the resolution into the engine's own expire and restore-rereferenced routes and retires
the probe; these tests pin the interim.

The real-engine tests build the shape (move under the pre-rewrite name, rewrite the row, run the pass) and
assert on the sibling's contents. Both pre-rewrite owners are covered: one whose sibling name equals the
reaper's own name for the origin (reachable by name alone, by both callers) and one that equals neither the
reaper's name nor today's (reachable only through what the engine resolves from the chunks'
``origin_collection`` tag, so only by ``nx t3 gc``).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.error import URLError

import pytest

import nexus.catalog.chunk_quarantine as cq
from nexus.db.http_vector_client import VectorServiceError

ORIGIN_SUFFIX = "__bge-base-en-v15-768__v1"
REWRITTEN_OWNER = "rewritten-1-9"
PROBE_PATH = "/v1/vectors/gc/quarantine-restore"


def _row(owner: str, model: str = "bge-base-en-v15-768") -> dict:
    return {"content_type": "code", "owner_id": owner, "embedding_model": model}


def _long_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── real engine ──────────────────────────────────────────────────────────────

#: (case id, owner the catalog row held when the client MOVED the chunks, whether ``nx index repo``'s two names
#: reach the sibling). "name-owner" is the owner in the origin's own name, so the sibling is the reaper's name;
#: "legacy-owner" is neither the reaper's name nor the one derived after the rewrite.
PRE_REWRITE_OWNERS = [("name-owner", None, True), ("legacy-owner", "legacy-owner", False)]


class _Scenario:
    """A repo collection whose orphans a client moved under its pre-rewrite sibling name, then whose catalog
    row's ``owner_id`` was rewritten (the catalog-044-3 shape). ``sibling`` names the collection the move goes
    into instead of the row-derived one (two origins sharing one sibling); ``n_orphans`` sizes the orphan set."""

    def __init__(
        self, cat, db, monkeypatch, case: str, pre_owner: str | None,
        *, n_orphans: int = 4, sibling: str | None = None,
    ) -> None:
        import nexus.mcp_infra as mcp_infra
        from tests._chunk_seed import seed_chunks_direct
        from tests._reapable_age import age_chunks_past_grace

        self.cat, self.db = cat, db
        self.slug = f"gcq-s3-{case}"
        self.coll = f"code__{self.slug}{ORIGIN_SUFFIX}"
        self.owner = cat.register_owner(self.slug, "curator")
        self.orphans = [hashlib.sha256(f"{self.coll}:orphan:{i}".encode()).hexdigest() for i in range(n_orphans)]
        self.live = [hashlib.sha256(f"{self.coll}:live:{i}".encode()).hexdigest() for i in range(2)]
        chashes = self.live + self.orphans
        seed_chunks_direct(
            self.coll, ids=chashes,
            documents=[f"def gcq_s3_{i}(): return {i}\n" for i in range(len(chashes))],
            metadatas=[{"chunk_text_hash": h, "title": f"gcq_s3_{i}.py:1-1"} for i, h in enumerate(chashes)],
        )
        age_chunks_past_grace(self.coll)
        for i, chash in enumerate(self.live):
            tumbler = str(cat.register(
                self.owner, f"gcq_s3_live_{i}.py", content_type="code",
                file_path=f"/tmp/{self.coll}/gcq_s3_live_{i}.py", physical_collection=self.coll, chunk_count=1,
            ))
            cat.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=self.coll)

        # The catalog row the client reads, as it was when the client moved the chunks...
        self._rows = {self.coll: _row(pre_owner or self.slug)}
        real = mcp_infra.get_collection_row
        monkeypatch.setattr(
            mcp_infra, "get_collection_row",
            lambda name, **kw: self._rows[name] if name in self._rows else real(name, **kw),
        )
        self.moved_into = sibling or cq.quarantine_collection_name(self.coll)
        moved = cq.quarantine_orphans_serverside(db, self.coll, self.moved_into, _long_ago())
        assert moved is not None and moved[0] == len(self.orphans), moved
        assert set(self.orphans) <= self.ids(self.moved_into), "setup: the orphans moved under the pre-rewrite name"

        # ...and after catalog-044-3 rewrote its owner_id.
        self._rows[self.coll] = _row(REWRITTEN_OWNER)
        self.derived_now = cq.quarantine_collection_name(self.coll)
        # Non-vacuity: the row-derived name no longer reaches where the chunks are.
        assert self.derived_now != self.moved_into

    @property
    def reaper_name(self) -> str:
        return f"quarantine-{self.coll}"

    def add_orphans(self, n: int, *, into: str) -> list[str]:
        """*n* more orphans, aged past the grace window and moved by the client into *into*."""
        from tests._chunk_seed import seed_chunks_direct
        from tests._reapable_age import age_chunks_past_grace

        more = [hashlib.sha256(f"{self.coll}:extra:{into}:{i}".encode()).hexdigest() for i in range(n)]
        seed_chunks_direct(
            self.coll, ids=more, documents=[f"def gcq_s3_extra_{h[:8]}(): return 1\n" for h in more],
            metadatas=[{"chunk_text_hash": h, "title": f"extra_{h[:6]}.py:1-1"} for h in more],
        )
        age_chunks_past_grace(self.coll)
        moved = cq.quarantine_orphans_serverside(self.db, self.coll, into, _long_ago())
        assert moved is not None and moved[0] == n, moved
        return more

    def ids(self, collection: str) -> set[str]:
        try:
            return set(self.db.get_collection(collection).get_all_metadata(include_non_live=True)["ids"])
        except Exception:  # noqa: BLE001 — a sibling that was never registered holds nothing
            return set()

    def reference(self, chashes: list[str]) -> None:
        """A heal re-writes *chashes* into the origin and gives each a manifest row again."""
        from tests._chunk_seed import seed_chunks_direct

        seed_chunks_direct(
            self.coll, ids=chashes,
            documents=[f"def gcq_s3_healed_{h[:6]}(): return 0\n" for h in chashes],
            metadatas=[{"chunk_text_hash": h, "title": f"healed_{h[:6]}.py:1-1"} for h in chashes],
        )
        for i, chash in enumerate(chashes):
            tumbler = str(self.cat.register(
                self.owner, f"gcq_s3_healed_{i}.py", content_type="code",
                file_path=f"/tmp/{self.coll}/gcq_s3_healed_{i}.py", physical_collection=self.coll, chunk_count=1,
            ))
            self.cat.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=self.coll)


@pytest.fixture
def engine(t2_service_env):
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog

    return ActiveCatalog(), hvc.HttpVectorClient(tenant=t2_service_env)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every engine path the client POSTs to, recorded as the real call goes through."""
    import nexus.db.http_vector_client as hvc

    paths: list[str] = []
    real_post = hvc._post

    def recording_post(path, body, **kw):
        paths.append(path)
        return real_post(path, body, **kw)

    monkeypatch.setattr(hvc, "_post", recording_post)
    return paths


def _gc(db, s: _Scenario) -> None:
    """``nx t3 gc``'s expiry step, as the verb calls it (row-derived name, probe on)."""
    from nexus.commands.t3 import _expire_client_quarantine

    _expire_client_quarantine(db, s.coll, s.derived_now, moved=0)


@pytest.mark.parametrize(("case", "pre_owner", "index_reaches"), PRE_REWRITE_OWNERS)
def test_index_repo_expiry_reaches_the_reaper_named_sibling_and_nx_t3_gc_reaches_the_rest(
    engine, monkeypatch: pytest.MonkeyPatch, wire: list[str], capsys: pytest.CaptureFixture[str],
    case: str, pre_owner: str | None, index_reaches: bool,
) -> None:
    from nexus.indexer import _prune_deleted_files

    cat, db = engine
    s = _Scenario(cat, db, monkeypatch, case, pre_owner)

    _prune_deleted_files(s.coll, "docs__gcq-s3-unused", db, catalog=cat)

    assert PROBE_PATH not in wire, f"an index-path prune sent the engine probe: {wire}"
    if index_reaches:
        assert s.ids(s.moved_into) == set(), (
            "the reaper-named sibling is one of the two names the index path uses; "
            f"still in {s.moved_into}: {s.ids(s.moved_into)}"
        )
    else:
        assert s.ids(s.moved_into) == set(s.orphans), (
            "a sibling under a third name is NOT reached by the index path (no probe on the hot path, "
            "nexus-wbfpw.64 retires the gap); it waits for nx t3 gc"
        )
        capsys.readouterr()
        _gc(db, s)
        assert s.ids(s.moved_into) == set(), "nx t3 gc asks the engine and reaches it"
        out = capsys.readouterr().out
        assert s.moved_into in out and f"{len(s.orphans)} expired" in out, out
    assert s.ids(s.coll) == set(s.live), "expiry touches nothing the origin holds"


@pytest.mark.parametrize(("case", "pre_owner", "index_reaches"), PRE_REWRITE_OWNERS)
def test_index_repo_rereference_reaches_only_the_two_derived_names(
    engine, monkeypatch: pytest.MonkeyPatch, wire: list[str], capsys: pytest.CaptureFixture[str],
    case: str, pre_owner: str | None, index_reaches: bool,
) -> None:
    from nexus.indexer import _prune_deleted_files

    cat, db = engine
    s = _Scenario(cat, db, monkeypatch, f"rr-{case}", pre_owner)
    healed, still_orphan = s.orphans[:2], s.orphans[2:]
    s.reference(healed)
    # Recent, so expiry cannot be what empties the sibling: only the re-reference restore can remove these two.
    assert s.ids(s.moved_into) == set(s.orphans)
    monkeypatch.setenv("NX_GC_QUARANTINE_DAYS", "3650")

    _prune_deleted_files(s.coll, "docs__gcq-s3-unused", db, catalog=cat)

    assert PROBE_PATH not in wire
    if index_reaches:
        assert s.ids(s.moved_into) == set(still_orphan), (
            "a chunk the manifest references again must leave the reaper-named sibling; "
            f"left in {s.moved_into}: {s.ids(s.moved_into)}"
        )
        assert set(healed) <= s.ids(s.coll)
    else:
        # Re-reference is indexer-only and uses the two names, so a third-name sibling keeps the healed chunks
        # hidden; nx t3 gc has no re-reference and its expiry keeps what the manifest references again.
        assert s.ids(s.moved_into) == set(s.orphans)
        monkeypatch.delenv("NX_GC_QUARANTINE_DAYS")
        capsys.readouterr()
        _gc(db, s)
        assert s.ids(s.moved_into) == set(healed), "gc expires the unreferenced two and keeps the healed two"
        out = capsys.readouterr().out
        assert "2 expired, 2 refused (kept: the manifest references them again" in out, out


@pytest.mark.parametrize(("case", "pre_owner", "index_reaches"), PRE_REWRITE_OWNERS)
def test_nx_t3_gc_expiry_reaches_chunks_moved_under_the_pre_rewrite_name(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    case: str, pre_owner: str | None, index_reaches: bool,
) -> None:
    """Both call sites in ``nx t3 gc`` (the nothing-to-move run and the post-move run) hand
    ``_expire_client_quarantine`` the row-derived name, which is what this passes."""
    cat, db = engine
    s = _Scenario(cat, db, monkeypatch, f"gc-{case}", pre_owner)

    _gc(db, s)

    assert s.ids(s.moved_into) == set()
    out = capsys.readouterr().out
    assert s.moved_into in out and f"{len(s.orphans)} expired" in out, out


def test_a_shared_sibling_never_loses_another_origins_tagged_rows(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Two origins, one sibling (what the row rule gave a slug collection and its conformant twin), plus a row
    with no ``origin_collection`` tag. The engine's expiry and restore read ``COALESCE(tag, <the origin being
    processed>)``, so:

    * a row tagged for ANOTHER origin is never expired by, or restored into, this one (asserted for both the
      re-reference restore and the expiry, through ``nx index repo``'s pass and ``nx t3 gc``'s);
    * a row with NO tag is read as the processing origin's own, so the first origin to expire takes it. That is
      pinned here rather than implied. The client's own move always tags (the move statement writes the tag),
      so an untagged row exists only if something else wrote it; it is not a state the client produces.

    The dry-run sibling probe is also read-only on the real engine: it writes no ``gc_audit`` row.
    """
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.indexer import _prune_deleted_files
    from tests._chunk_seed import seed_chunks_direct

    cat, db = engine
    s1 = _Scenario(cat, db, monkeypatch, "xo1", None)
    s2 = _Scenario(cat, db, monkeypatch, "xo2", None, sibling=s1.moved_into)
    shared = s1.moved_into
    assert s2.moved_into == shared and shared == s1.reaper_name
    untagged = hashlib.sha256(b"gcq-s3-untagged").hexdigest()
    seed_chunks_direct(
        shared, ids=[untagged], documents=["def gcq_s3_untagged(): return 9\n"],
        metadatas=[{"title": "untagged.py:1-1", "quarantined_at": _long_ago()}],
    )
    both = set(s1.orphans) | set(s2.orphans)
    assert s1.ids(shared) == both | {untagged}, "setup: one sibling holds both origins' rows and an untagged one"

    # The probe is a dry-run: it finds the shared sibling and writes no audit row.
    def audit_count() -> int:
        return len(HttpCatalogClient().gc_audit_list(limit=300))

    before = audit_count()
    assert before > 0, "non-vacuity: the two client moves left audit rows, so a new row would show"
    found = cq.resolve_quarantine_siblings(db, s1.coll, primary=s1.derived_now, probe_engine=True)
    assert shared in found
    assert audit_count() == before, "the dry-run probe wrote a gc_audit row"

    # Re-reference: s1's manifest references a chash that sits in the shared sibling tagged for s2.
    foreign = s2.orphans[0]
    s1.reference([foreign])
    monkeypatch.setenv("NX_GC_QUARANTINE_DAYS", "3650")
    _prune_deleted_files(s1.coll, "docs__gcq-s3-unused", db, catalog=cat)
    assert s1.ids(shared) == both | {untagged}, "restore into s1 took nothing tagged for another origin"
    assert foreign not in s2.ids(s2.coll)

    # Expiry from s1 (nx t3 gc: derived name, reaper name, and what the engine resolves).
    monkeypatch.delenv("NX_GC_QUARANTINE_DAYS")
    capsys.readouterr()
    _gc(db, s1)
    assert s1.ids(shared) == set(s2.orphans), (
        "s1's expiry takes s1's own four and the untagged row (COALESCE reads it as s1's), "
        f"and leaves every row tagged for s2; left: {s1.ids(shared)}"
    )
    assert f"{len(s1.orphans) + 1} expired" in capsys.readouterr().out

    # s2's expiry then takes its own.
    _gc(db, s2)
    assert s1.ids(shared) == set()


def test_a_bulk_aged_quarantine_expires_through_nx_t3_gc_with_no_force(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The client's expiry of rows already past the restore window carries no fraction floor (nexus-wbfpw.74,
    Sam 2026-10-03), matching the engine's own expiry: the engine function already keeps every chash the
    origin's manifest still references, so a floor only wedged every bulk quarantine (one burst ages out as
    ~100% of the sibling's client rows). A legacy-owner sibling of 110 unreferenced rows, all of the sibling's
    client rows, expires with no NX_GC_FORCE; a different sibling's small expiry is reported as before."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    cat, db = engine
    s = _Scenario(cat, db, monkeypatch, "floor", "legacy-owner", n_orphans=110)
    legacy = s.moved_into
    small = s.add_orphans(3, into=s.reaper_name)
    assert legacy != s.reaper_name and len(s.ids(legacy)) == 110

    _gc(db, s)

    out = capsys.readouterr().out
    legacy_line = next(line for line in out.splitlines() if f"Client expiry of {legacy} " in line)
    small_line = next(line for line in out.splitlines() if f"Client expiry of {s.reaper_name} " in line)
    assert "110 expired, 0 refused" in legacy_line and "kept" not in legacy_line
    assert "3 expired, 0 refused" in small_line and "kept" not in small_line
    assert "FLOOR" not in out and "FORCE" not in out, out
    assert s.ids(legacy) == set(), "the bulk sibling expired every row"
    assert s.ids(s.reaper_name) == set(), f"the small sibling expired its {len(small)} rows"
    assert s.ids(s.coll) == set(s.live), "expiry touches nothing the origin holds"


def test_a_bulk_aged_quarantine_keeps_what_the_manifest_references_through_nx_t3_gc(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """No floor does not mean no protection: the 5 rows a heal re-referenced stay, and the line says they were
    kept because the manifest references them, never because of a floor."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    cat, db = engine
    s = _Scenario(cat, db, monkeypatch, "bulk-ref-gc", "legacy-owner", n_orphans=110)
    healed = s.orphans[:5]
    s.reference(healed)

    _gc(db, s)

    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if f"Client expiry of {s.moved_into} " in ln)
    assert "105 expired, 5 refused (kept: the manifest references them again)" in line, line
    assert "FLOOR" not in out and "FORCE" not in out, out
    assert s.ids(s.moved_into) == set(healed)


def test_a_bulk_aged_quarantine_expires_through_the_index_gc_pass_with_no_force(
    engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, through ``nx index repo``'s GC pass (``_prune_deleted_files``): 110 unreferenced rows past the
    restore window, 100% of the sibling's client rows, expire. The pass restores the 5 rows the manifest
    references again before it expires, so those leave the sibling by restore, not by deletion."""
    from nexus.indexer import _prune_deleted_files

    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    cat, db = engine
    s = _Scenario(cat, db, monkeypatch, "bulk-ref-idx", None, n_orphans=110)
    healed = s.orphans[:5]
    s.reference(healed)
    assert s.ids(s.moved_into) == set(s.orphans)
    _prune_deleted_files(s.coll, "docs__gcq-s3-unused", db, catalog=cat)

    assert s.ids(s.moved_into) == set(), "110 aged rows left the sibling with no NX_GC_FORCE"
    assert s.ids(s.coll) == set(s.live) | set(healed), "the 5 referenced rows are in the origin, the rest are gone"


# ── where the engine cannot answer ───────────────────────────────────────────

ORIGIN = f"code__nexus-1-1{ORIGIN_SUFFIX}"
REAPER_NAME = f"quarantine-{ORIGIN}"
ROW_NAME = f"quarantine-code__{REWRITTEN_OWNER}{ORIGIN_SUFFIX}"
THIRD = "quarantine-code__legacy__x__v1"


class _Db:
    """A T3 handle recording the sibling probe; ``reply`` is its answer (or the exception it raises)."""

    def __init__(self, reply) -> None:
        self.reply, self.probes = reply, []

    def gc_quarantine_restore(self, origin, **kwargs):
        self.probes.append((origin, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def rewritten_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.mcp_infra as mcp_infra

    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, **kw: _row(REWRITTEN_OWNER))


def test_a_handle_with_no_restore_route_gets_the_row_derived_and_reaper_names(rewritten_row) -> None:
    assert cq.resolve_quarantine_siblings(object(), ORIGIN, probe_engine=True) == [ROW_NAME, REAPER_NAME]


def test_the_engine_is_not_probed_unless_asked(rewritten_row) -> None:
    """A new caller must not get the engine scan by accident: ``probe_engine`` defaults to False."""
    db = _Db({"quarantine_collections": [THIRD]})

    assert cq.resolve_quarantine_siblings(db, ORIGIN) == [ROW_NAME, REAPER_NAME]
    assert cq.resolve_quarantine_siblings(db, ORIGIN, probe_engine=False) == [ROW_NAME, REAPER_NAME]
    assert db.probes == []


def test_the_engine_resolved_siblings_join_the_names_without_duplicates(rewritten_row) -> None:
    db = _Db({"quarantine_collections": [REAPER_NAME, THIRD]})

    assert cq.resolve_quarantine_siblings(db, ORIGIN, probe_engine=True) == [ROW_NAME, REAPER_NAME, THIRD]
    ((origin, kwargs),) = db.probes
    assert origin == ORIGIN
    # The probe selects nothing and changes nothing.
    assert kwargs["dry_run"] is True and kwargs["after_chash"] == "f" * 64 and kwargs["limit"] == 1


@pytest.mark.parametrize("code", [400, 404, 422, 500, 503])
def test_a_refused_probe_falls_back_to_the_names_and_says_so(rewritten_row, code: int) -> None:
    db = _Db(VectorServiceError("no", code=code))

    with patch.object(cq, "_log") as log:
        assert cq.resolve_quarantine_siblings(db, ORIGIN, probe_engine=True) == [ROW_NAME, REAPER_NAME]

    expected = code in (400, 404, 422)
    emitted = (log.info if expected else log.warning).call_args_list
    (call,) = emitted
    assert call.args == ("quarantine_sibling_probe_unavailable",)
    assert call.kwargs["code"] == code and call.kwargs["collection"] == ORIGIN
    assert not (log.warning if expected else log.info).called


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError, URLError])
def test_a_probe_transport_failure_falls_back_to_the_names(rewritten_row, error: type[Exception]) -> None:
    """Without a managed endpoint the client re-raises a bare transport error (all OSError), and a timeout is
    the unbounded probe's likeliest failure: it must fall back like an engine refusal, not crash nx t3 gc."""
    db = _Db(error("probe timed out"))

    with patch.object(cq, "_log") as log:
        assert cq.resolve_quarantine_siblings(db, ORIGIN, probe_engine=True) == [ROW_NAME, REAPER_NAME]

    (call,) = log.warning.call_args_list
    assert call.args == ("quarantine_sibling_probe_unavailable",) and call.kwargs["code"] is None


def test_an_explicit_primary_leads_the_set(rewritten_row) -> None:
    assert cq.resolve_quarantine_siblings(object(), ORIGIN, primary="quarantine-x") == ["quarantine-x", REAPER_NAME]


def test_expiry_runs_over_every_sibling_and_sums_the_counts() -> None:
    seen: list[str] = []

    class Db:
        def gc_expire_quarantine(self, quarantine, origin, cutoff, fraction, minimum, force):
            seen.append(quarantine)
            return {"expired": 2, "refused": 1}

    assert cq.expire_quarantine_across_serverside(
        Db(), ["quarantine-a", "quarantine-b"], ORIGIN, "2026-01-01T00:00:00Z",
    ) == (4, 2)
    assert seen == ["quarantine-a", "quarantine-b"]


def test_expiry_with_no_route_is_none_not_zero() -> None:
    assert cq.expire_quarantine_across_serverside(
        object(), ["quarantine-a"], ORIGIN, "2026-01-01T00:00:00Z",
    ) is None


def test_restore_runs_over_every_sibling_and_sums_the_counts() -> None:
    seen: list[str] = []

    class Db:
        def gc_restore_rereferenced_bounded(self, quarantine, origin, row_limit):
            seen.append(quarantine)
            return {"restored": 3, "remaining": 0}

    assert cq.restore_rereferenced_across_serverside(Db(), ["quarantine-a", "quarantine-b"], ORIGIN) == 6
    assert seen == ["quarantine-a", "quarantine-b"]
    assert cq.restore_rereferenced_across_serverside(object(), ["quarantine-a"], ORIGIN) is None
    assert cq.restore_rereferenced_across_serverside(object(), [], ORIGIN) == 0


# ── the index path: names only, extras are best-effort ───────────────────────

class _IndexDb:
    """A T3 handle with every route ``_prune_collection_serverside`` drives. ``fail`` names the (route, sibling)
    pairs that answer an engine error; the sibling probe fails the test if it is ever called."""

    def __init__(self, fail: frozenset[tuple[str, str]] = frozenset(), error: type[Exception] | None = None) -> None:
        self.fail, self.calls, self.error = fail, [], error

    def _hit(self, route: str, sibling: str) -> None:
        self.calls.append((route, sibling))
        if (route, sibling) in self.fail:
            if self.error is not None:
                raise self.error(f"{route} {sibling} timed out")
            raise VectorServiceError(f"{route} {sibling} refused", code=500)

    def gc_quarantine_restore(self, *a, **kw):
        raise AssertionError("an index-path prune must not send the sibling probe")

    def gc_restore_rereferenced_bounded(self, quarantine, origin, row_limit):
        self._hit("restore", quarantine)
        return {"restored": 1, "remaining": 0}

    def gc_quarantine_orphans_bounded(self, origin, quarantine, at, sample, row_limit):
        self._hit("move", quarantine)
        return {"moved": 2, "sample": [], "remaining": 0}

    def gc_expire_quarantine(self, quarantine, origin, cutoff, fraction, minimum, force):
        self._hit("expire", quarantine)
        return {"expired": 3, "refused": 0}


def _prune(db: _IndexDb, primary: str = ROW_NAME) -> bool:
    from nexus.indexer import _prune_collection_serverside

    return _prune_collection_serverside(db, ORIGIN, primary, "2026-01-01T00:00:00Z")


def test_an_index_path_prune_uses_the_two_names_and_sends_no_probe(rewritten_row) -> None:
    db = _IndexDb()

    assert _prune(db) is True

    assert db.calls == [
        ("restore", ROW_NAME), ("restore", REAPER_NAME),
        ("move", ROW_NAME),
        ("expire", ROW_NAME), ("expire", REAPER_NAME),
    ]


def test_a_failing_extra_sibling_never_costs_the_move_or_the_primary(rewritten_row) -> None:
    db = _IndexDb(fail=frozenset({("restore", REAPER_NAME), ("expire", REAPER_NAME)}))

    with patch.object(cq, "_log") as log:
        assert _prune(db) is True

    assert ("move", ROW_NAME) in db.calls and ("expire", ROW_NAME) in db.calls
    events = {c.args[0]: c.kwargs["sibling"] for c in log.warning.call_args_list}
    assert events == {
        "quarantine_sibling_restore_failed": REAPER_NAME, "quarantine_sibling_expire_failed": REAPER_NAME,
    }


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError])
def test_an_extra_sibling_transport_failure_never_costs_the_move_or_the_primary(
    rewritten_row, error: type[Exception],
) -> None:
    db = _IndexDb(fail=frozenset({("restore", REAPER_NAME), ("expire", REAPER_NAME)}), error=error)

    with patch.object(cq, "_log") as log:
        assert _prune(db) is True

    assert ("move", ROW_NAME) in db.calls and ("expire", ROW_NAME) in db.calls
    events = {c.args[0]: c.kwargs["sibling"] for c in log.warning.call_args_list}
    assert events == {
        "quarantine_sibling_restore_failed": REAPER_NAME, "quarantine_sibling_expire_failed": REAPER_NAME,
    }


def test_a_primary_transport_failure_still_raises(rewritten_row) -> None:
    with pytest.raises(TimeoutError):
        cq.expire_quarantine_across_serverside(
            _IndexDb(fail=frozenset({("expire", ROW_NAME)}), error=TimeoutError), [ROW_NAME, REAPER_NAME], ORIGIN,
            "2026-01-01T00:00:00Z", best_effort=True,
        )


def test_a_failing_primary_sibling_still_raises(rewritten_row) -> None:
    """The primary's failure behaviour is today's: it propagates (``_prune_deleted_files`` logs and goes on)."""
    with pytest.raises(VectorServiceError):
        _prune(_IndexDb(fail=frozenset({("restore", ROW_NAME)})))
    with pytest.raises(VectorServiceError):
        _prune(_IndexDb(fail=frozenset({("expire", ROW_NAME)})))


# ── nx t3 gc: one line per sibling, and what earlier siblings did survives a later failure ──

class _GcDb:
    def __init__(self, siblings: list[str], expire: dict[str, dict | Exception]) -> None:
        self.siblings, self.expire = siblings, expire

    def gc_quarantine_restore(self, origin, **kwargs):
        return {"quarantine_collections": self.siblings}

    def gc_expire_quarantine(self, quarantine, origin, cutoff, fraction, minimum, force):
        reply = self.expire.get(quarantine, {"expired": 0, "refused": 0})
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_nx_t3_gc_prints_each_sibling_it_tried_with_its_own_outcome(
    rewritten_row, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.commands.t3 import _expire_client_quarantine

    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    db = _GcDb([REAPER_NAME, THIRD], {
        REAPER_NAME: {"expired": 4, "refused": 2},
        THIRD: {"expired": 0, "refused": 120},
    })

    _expire_client_quarantine(db, ORIGIN, ROW_NAME, moved=0)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  Client expiry of")]
    assert [ln.split()[3] for ln in lines] == [ROW_NAME, REAPER_NAME, THIRD]
    assert "0 expired, 0 refused" in lines[0] and "kept" not in lines[0]
    assert "4 expired, 2 refused (kept: the manifest references them again)" in lines[1]
    assert "0 expired, 120 refused (kept: the manifest references them again)" in lines[2]
    assert not any("FLOOR" in ln or "FORCE" in ln for ln in lines)


def test_a_failure_on_a_later_sibling_still_reports_what_the_earlier_ones_expired(
    rewritten_row, capsys: pytest.CaptureFixture[str],
) -> None:
    from click.exceptions import Exit

    from nexus.commands.t3 import _expire_client_quarantine

    db = _GcDb([REAPER_NAME, THIRD], {REAPER_NAME: {"expired": 4, "refused": 0},
                                      THIRD: VectorServiceError("engine went away", code=500)})

    with pytest.raises(Exit):
        _expire_client_quarantine(db, ORIGIN, ROW_NAME, moved=7)

    cap = capsys.readouterr()
    assert "4 expired, 0 refused" in cap.out
    assert "the move succeeded (7 chunk(s) quarantined)" in cap.err
    assert f"expiry of {THIRD} FAILED" in cap.err
    assert f"Already expired before it: 0 from {ROW_NAME}, 4 from {REAPER_NAME}." in cap.err


def test_a_handle_with_no_expire_route_says_so_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus.commands.t3 import _expire_client_quarantine

    _expire_client_quarantine(object(), ORIGIN, ROW_NAME, moved=0)

    cap = capsys.readouterr()
    assert "did NOT run" in cap.err and "gc_expire_quarantine" in cap.err and cap.out == ""
