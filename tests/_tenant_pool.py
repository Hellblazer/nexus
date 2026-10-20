# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-worker data-reset tenant pool for the engine substrate (nexus-fpwj1, rtg20 phase B).

``_engine_tenant_env`` mints a tenant for every test that touches storage and drops its partition leaves afterwards:
about 50 relations created and dropped per test. The pool is ON by default (nexus-7zloy): a worker mints ONE tenant,
lazily, on its first lease, and every later lease re-uses it after its data is reset. ``NX_TEST_TENANT_POOL=0`` turns it
off; any other value, including unset and ``1``, leaves it on.

Teardown of a lease, in this order (design: T2 ``nexus/design-nexus-rtg20-data-reset-pool-2026-10-18``; route:
nexus-0bl77):

1. one psql transaction as the substrate superuser: ``session_replication_role = replica``, a per-tenant advisory
   lock, ``DELETE ... WHERE tenant_id = $1`` over every tenant-keyed relation (derived from the catalog at call
   time, token rows included). No DDL, no ``DROP INDEX``, no ``TRUNCATE``.
2. ``POST /v1/_test/tenant-reset``: the engine evicts every JVM structure keyed by the tenant (token cache, collection
   registry, ghost-sweep marker, rate limiters, parked tuple waiters).
3. census: at least ``MIN_CENSUS_RELATIONS`` relations examined, every ``REQUIRED_CENSUS_RELATIONS`` member among them,
   zero rows for the tenant in each, zero ``pci_`` indexes on its leaves. It runs AFTER the wipe in the same teardown,
   so it proves the wipe completed and the tenant was quiet through the teardown window (a writer committing between
   the wipe and the census is caught). It cannot see rows a test left before the wipe: the wipe deletes them.
4. ``POST /v1/tenants/create`` for the same name: the token rows are gone, so this issues a fresh bearer (the
   service_tokens trigger finds the leaves and creates nothing).

A lease also starts with a census (before the test body, the tenant's one allowed token row aside): a row there was
committed by a writer that fired between the previous lease's teardown and this setup, and the NEW test's setup
errors naming the PREVIOUS lease's owner as the likely leaker. A writer that fires during the next test's body is still
undetected here; that residual belongs to the A/B failure-set diff (nexus-7zloy).

ANY failure of a teardown step (a leak verdict, an httpx error, a psql timeout, a malformed re-issue reply) retires the
tenant (best-effort wipe, ``drop_test_tenant``, a fresh mint on the next lease, counted) and raises
:class:`TenantLeakError` from the fixture teardown naming the cause, so the test whose teardown failed ERRORS. Python
caches that outlive a test and key on something other than the bearer are reset on every lease.

Every SQL statement runs as the substrate's bootstrap superuser (the engine roles cannot run it and it must bypass
FORCE ROW LEVEL SECURITY). A census that examined nothing is a failure, not a pass (the nexus-moht0 doctrine).

The module is also a pytest plugin (registered from ``tests/conftest.py``): when the flag is on, each worker's final
census, lease and retire counts reach the controller, which prints one ``tenant pool:`` line and fails the run when a
worker's pool tenant is not empty at the end, or when tenant tests ran and no lease was taken (non-vacuity).
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import structlog

_log = structlog.get_logger(__name__)

ENV_FLAG = "NX_TEST_TENANT_POOL"
#: Engine env that registers ``POST /v1/_test/tenant-reset`` (exactly ``"1"``; nexus-0bl77).
ROUTE_ENV = "NX_TEST_TENANT_RESET_ROUTE"
MARKER = "fresh_tenant"

#: The census refuses to pass over fewer relations than this (the live substrate has about 50), and over a set that
#: lacks any of these.
MIN_CENSUS_RELATIONS = 40
REQUIRED_CENSUS_RELATIONS = (
    "nexus.chunks",
    "nexus.taxonomy_centroids",
    "nexus.memory",
    "nexus.catalog_collections",
    "nexus.service_tokens",
    "nexus.session_tokens",
    "t1.scratch",
    "nexus.tuples",
    "nexus.request_ledger",
)

#: Prefix of every census / wipe verdict this module's SQL raises; Python tells a verdict from any other psql failure.
VERDICT = "nx-tenant-pool:"

_WORKEROUTPUT_KEY = "nx_tenant_pool"
_STASH = pytest.StashKey[Any]()


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True unless ``NX_TEST_TENANT_POOL`` is exactly ``0``; unset (the default) and ``1`` mean on."""
    env = os.environ if environ is None else environ
    return env.get(ENV_FLAG, "") != "0"


class TenantLeakError(AssertionError):
    """A lease ended with the tenant not provably empty, or a reset step failed. Raised from fixture teardown, so
    the test that leaked is the one that errors."""


# Tenant-keyed, non-partition relations in nexus and t1. Used by wipe and census.
_TENANT_RELATIONS_SQL = """
SELECT n.nspname || '.' || c.relname AS qname, n.nspname AS ns, c.relname AS rel
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('nexus', 't1')
   AND c.relkind IN ('r', 'p')
   AND NOT c.relispartition
   AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0 AND NOT a.attisdropped)
"""

_CENSUS_BODY = f"""
    SELECT count(*) INTO v_n FROM ({_TENANT_RELATIONS_SQL}) rels;
    IF v_n < {MIN_CENSUS_RELATIONS} THEN
        RAISE EXCEPTION '{VERDICT} census examined % relations, fewer than {MIN_CENSUS_RELATIONS}', v_n;
    END IF;
    SELECT string_agg(req, ', ') INTO v_missing
      FROM unnest(ARRAY[{", ".join("'" + r + "'" for r in REQUIRED_CENSUS_RELATIONS)}]) AS req
     WHERE req NOT IN (SELECT qname FROM ({_TENANT_RELATIONS_SQL}) rels);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION '{VERDICT} census set is missing required relations: %', v_missing;
    END IF;
    -- Every relation a row-level-security policy guards must carry tenant_id, or the wipe cannot reach it.
    SELECT string_agg(pn.nspname || '.' || pc.relname, ', ') INTO v_missing
      FROM pg_catalog.pg_policy p
      JOIN pg_catalog.pg_class pc ON pc.oid = p.polrelid
      JOIN pg_catalog.pg_namespace pn ON pn.oid = pc.relnamespace
     WHERE pn.nspname IN ('nexus', 't1')
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                        WHERE a.attrelid = pc.oid AND a.attname = 'tenant_id' AND a.attnum > 0 AND NOT a.attisdropped);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION '{VERDICT} relations under a row-level-security policy have no tenant_id column: %', v_missing;
    END IF;
    -- The census itself: zero rows for the tenant everywhere it can live.
    SELECT pg_catalog.string_agg(
               pg_catalog.format('SELECT %L AS rel, count(*) AS n FROM %I.%I WHERE tenant_id = $1%s HAVING count(*) > 0',
                                 ns || '.' || rel, ns, rel,
                                 CASE WHEN qname = 'nexus.service_tokens' THEN ' AND token_hash <> $2' ELSE '' END),
               ' UNION ALL ' ORDER BY ns, rel)
      INTO v_sql
      FROM ({_TENANT_RELATIONS_SQL}) rels;
    EXECUTE 'SELECT pg_catalog.string_agg(rel || ''='' || n, '' '') FROM (' || v_sql || ') u' INTO v_left USING v_tenant, v_allow;
    IF v_left IS NOT NULL THEN
        RAISE EXCEPTION '{VERDICT} tenant % still has rows: %', v_tenant, v_left;
    END IF;
    -- The planner-side reconciler's per-leaf indexes are DDL a reset must never have to undo: assert none exist.
    SELECT string_agg(ic.relname, ', ') INTO v_left
      FROM pg_catalog.pg_class lc
      JOIN pg_catalog.pg_index ix ON ix.indrelid = lc.oid
      JOIN pg_catalog.pg_class ic ON ic.oid = ix.indexrelid
     WHERE lc.relispartition AND nexus.partition_bound_value(lc.oid) = v_tenant AND ic.relname LIKE 'pci\\_%';
    IF v_left IS NOT NULL THEN
        RAISE EXCEPTION '{VERDICT} tenant % has pci_ indexes on its leaves: %', v_tenant, v_left;
    END IF;
    RAISE NOTICE 'census examined % relations, 0 rows for tenant %', v_n, v_tenant;
"""

_CENSUS_DECLARE = (
    "v_tenant text := pg_catalog.current_setting('nx.pool_tenant'); "
    "v_allow text := pg_catalog.current_setting('nx.pool_allow_token'); "
    "v_n integer; v_missing text; v_left text; v_sql text;"
)

#: One transaction: lock, delete every tenant-keyed row, commit. DML only.
WIPE_SQL = f"""
\\set ON_ERROR_STOP on
BEGIN;
SET LOCAL session_replication_role = replica;
SET LOCAL lock_timeout = '10s';
SELECT pg_catalog.set_config('nx.pool_tenant', :'tenant', true);
DO $wipe$
DECLARE
    v_tenant text := pg_catalog.current_setting('nx.pool_tenant');
    r record;
BEGIN
    IF v_tenant IS NULL OR v_tenant = '' OR v_tenant = 'default' THEN
        RAISE EXCEPTION '{VERDICT} tenant wipe refuses tenant %', v_tenant;
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('nexus.create_tenant_partitions:' || v_tenant, 0));
    FOR r IN SELECT ns, rel FROM ({_TENANT_RELATIONS_SQL}) rels ORDER BY ns, rel LOOP
        EXECUTE pg_catalog.format('DELETE FROM %I.%I WHERE tenant_id = $1', r.ns, r.rel) USING v_tenant;
    END LOOP;
END
$wipe$;
COMMIT;
"""

#: Dead and live tuples and bytes over the tenant's ``nexus.chunks_m<model>_t_<tenant>`` leaves (one per embedding model).
LEAF_BLOAT_SQL = """
SELECT coalesce(sum(s.n_dead_tup), 0)::text || ' ' || coalesce(sum(s.n_live_tup), 0)::text || ' '
       || coalesce(sum(pg_catalog.pg_relation_size(c.oid)), 0)::text
  FROM pg_catalog.pg_class c
  LEFT JOIN pg_catalog.pg_stat_all_tables s ON s.relid = c.oid
 WHERE c.relnamespace = 'nexus'::regnamespace AND c.relname LIKE 'chunks\\_m%' AND c.relispartition
   AND nexus.partition_bound_value(c.oid) = :'tenant';
"""

#: The census alone, for a caller that wants the verdict on its own (the end-of-session check, the negative control).
#: ``allow_token`` is the one ``service_tokens.token_hash`` the census does not count: the bearer a reset re-issued,
#: which the tenant legitimately holds between leases. Empty counts every token row.
CENSUS_SQL = f"""
\\set ON_ERROR_STOP on
SELECT pg_catalog.set_config('nx.pool_tenant', :'tenant', false),
       pg_catalog.set_config('nx.pool_allow_token', :'allow_token', false);
DO $census$
DECLARE {_CENSUS_DECLARE}
BEGIN
{_CENSUS_BODY}
END
$census$;
"""


def _real_run() -> Callable[..., Any]:
    from tests import _engine_substrate  # noqa: PLC0415 — the module boots nothing at import, but is heavy

    return _engine_substrate._REAL_SUBPROCESS_RUN


def psql(state: Mapping[str, Any], sql: str, **variables: str) -> Any:
    """Run ``sql`` through the substrate's psql as its bootstrap superuser; ``variables`` become ``-v`` args."""
    argv = [
        str(Path(state["pg_bin"]) / "psql"), "-X", "-q", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
        "-U", state["pg_user"], "-d", state["pg_dbname"], "-t", "-A", "-f", "-",
    ]
    for name, value in variables.items():
        argv += ["-v", f"{name}={value}"]
    return _real_run()(
        argv, input=sql, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
    )


def _verdict_of(proc: Any) -> str:
    """The reason a psql run failed: the verdict line when this module's SQL raised one, else the tail of stderr."""
    text = (proc.stderr or proc.stdout or "").strip()
    for line in text.splitlines():
        if VERDICT in line:
            return line.split(VERDICT, 1)[1].strip()
    return text[-400:] or f"psql exit {proc.returncode}"


def wipe(state: Mapping[str, Any], tenant: str) -> None:
    """Delete every row of ``tenant`` (token rows included) in one transaction. Raises :class:`TenantLeakError`."""
    proc = psql(state, WIPE_SQL, tenant=tenant)
    if proc.returncode != 0:
        raise TenantLeakError(f"tenant {tenant}: wipe failed: {_verdict_of(proc)}")


def token_hash(token: str) -> str:
    """The ``service_tokens.token_hash`` of a raw bearer (hex sha256, as the engine stores it)."""
    return hashlib.sha256(token.encode()).hexdigest()


def census(state: Mapping[str, Any], tenant: str, *, allow_token: str = "") -> None:
    """Prove ``tenant`` is empty. Raises :class:`TenantLeakError` naming the relations that still hold rows (or
    saying the census set was too small or lacked a required relation). ``allow_token`` is a raw bearer whose
    ``service_tokens`` row is not counted."""
    proc = psql(state, CENSUS_SQL, tenant=tenant, allow_token=token_hash(allow_token) if allow_token else "")
    if proc.returncode != 0:
        raise TenantLeakError(f"tenant {tenant}: census failed: {_verdict_of(proc)}")


def reset_client_caches() -> None:
    """Drop the Python caches that outlive a test and are not keyed by the per-lease bearer.

    Looked up in ``sys.modules`` so a worker that never loaded a module imports nothing: an unloaded module holds
    no cache. ``corpus._REGISTERED_COLLECTIONS`` is already cleared per test by ``tests/conftest.py``.
    """
    mods = sys.modules
    hvc = mods.get("nexus.db.http_vector_client")
    if hvc is not None:
        hvc._invalidate_endpoint()
        hvc.reset_http_vector_client_for_tests()
    se = mods.get("nexus.search_engine")
    if se is not None:
        with se._poisoned_collections_lock:
            se._poisoned_collections.clear()
    ae = mods.get("nexus.aspect_extractor")
    if ae is not None:
        ae._ENGINE_ASPECTS_ENABLED_CACHE.clear()


class TenantLeaseBusyError(RuntimeError):
    """``lease()`` was called while another lease on the same pool was open."""


class TenantPool:
    """One worker's pool: one lazily minted tenant, leased to one test at a time (``lease`` enforces it).

    ``evict=False`` skips the engine route (step 2) and exists ONLY so a test can show that the eviction is what
    makes a collection registered in lease A unknown in lease B; ``start_census=False`` skips the lease-start census
    and exists ONLY so a test can show what that census catches.
    """

    def __init__(self, state: Mapping[str, Any], *, evict: bool = True,
                 mint: Callable[[Mapping[str, Any]], tuple[str, str]] | None = None,
                 drop: Callable[[Mapping[str, Any], str], bool] | None = None,
                 start_census: bool = True) -> None:
        from tests import _engine_substrate  # noqa: PLC0415

        self.state = state
        self.evict = evict
        self.start_census = start_census
        self._last_owner = ""
        self._mint = mint or _engine_substrate.mint_test_tenant
        self._drop = drop or _engine_substrate.drop_test_tenant
        self._lock = threading.Lock()
        self._leased = False
        self.tenant: str | None = None
        self.token: str | None = None
        self.leases = 0
        self.retired = 0
        self.mints = 0
        self.evicted: dict[str, int] = {}
        self.step_s: dict[str, float] = {}

    @contextlib.contextmanager
    def _timed(self, step: str) -> Iterator[None]:
        """Add the block's wall time to ``step_s[step]``; the A/B reads where a lease's cost goes from these."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.step_s[step] = self.step_s.get(step, 0.0) + (time.perf_counter() - t0)

    # -- engine calls -------------------------------------------------------------------------------------

    def _post(self, path: str, body: dict[str, str]) -> Any:
        import httpx  # noqa: PLC0415

        return httpx.post(
            f"{self.state['base_url']}{path}", json=body,
            headers={"Authorization": f"Bearer {self.state['bearer']}", "Content-Type": "application/json"},
            timeout=60.0,
        )

    def _issue_token(self, tenant: str) -> str:
        resp = self._post("/v1/tenants/create", {"name": tenant})
        if resp.status_code != 200 or not resp.json().get("token"):
            raise TenantLeakError(f"tenant {tenant}: token re-issue failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json()["token"]

    def _evict(self, tenant: str) -> None:
        resp = self._post("/v1/_test/tenant-reset", {"tenant": tenant})
        if resp.status_code != 200:
            raise TenantLeakError(
                f"tenant {tenant}: POST /v1/_test/tenant-reset answered {resp.status_code}: {resp.text[:200]}"
                f" (is {ROUTE_ENV}=1 set on the substrate engine?)"
            )
        for name, count in (resp.json().get("evicted") or {}).items():
            self.evicted[name] = self.evicted.get(name, 0) + int(count)

    # -- lifecycle ----------------------------------------------------------------------------------------

    def reset(self, tenant: str) -> str:
        """The four teardown steps in order; returns the re-issued bearer. Raises :class:`TenantLeakError`."""
        with self._timed("wipe"):
            wipe(self.state, tenant)
        if self.evict:
            with self._timed("evict"):
                self._evict(tenant)
        with self._timed("census"):
            census(self.state, tenant)
        with self._timed("reissue"):
            return self._issue_token(tenant)

    def retire(self, reason: str) -> None:
        """Forget the tenant (best-effort wipe and partition drop) so the next lease mints a new one. Counted."""
        tenant, self.tenant, self.token, self._last_owner = self.tenant, None, None, ""
        if tenant is None:
            return
        self.retired += 1
        _log.warning("tenant_pool_retired", tenant=tenant, reason=reason)
        with contextlib.suppress(Exception):
            wipe(self.state, tenant)
        with contextlib.suppress(Exception):
            self._evict(tenant)
        with contextlib.suppress(Exception):
            self._drop(self.state, tenant)

    @contextlib.contextmanager
    def lease(self, owner: str = "") -> Iterator[tuple[str, str]]:
        """Hand out ``(tenant, token)``; reset the tenant however the block ends.

        ``owner`` names the leasing test (its node id); the next lease's start census cites it when it finds rows.

        One lease at a time: a second ``lease()`` while one is open raises :class:`TenantLeaseBusyError` (it would
        otherwise share, and at its end wipe, the other test's rows).

        ANY exception from the reset (a leak verdict, an httpx error, a psql timeout, a malformed re-issue reply)
        retires the tenant, so the next lease mints a fresh one and never receives a deleted bearer or unwiped rows.
        When the block ended cleanly the failure is raised, naming its cause, so the leasing test errors at
        teardown; when the block raised, the block's own exception is the one that propagates and the teardown
        failure is logged.
        """
        with self._lock:
            if self._leased:
                raise TenantLeaseBusyError(
                    f"tenant {self.tenant}: lease() called while a lease is open; the pool serves one test at a time")
            self._leased = True
            try:
                if self.tenant is None:
                    self.tenant, self.token = self._mint(self.state)
                    self.mints += 1
                tenant, token = self.tenant, self.token
                self.leases += 1
            except BaseException:
                self._leased = False
                raise
        try:
            with self._timed("caches"):
                reset_client_caches()
            self._census_at_start(tenant, token, owner)
            completed = False
            try:
                yield tenant, token  # type: ignore[misc]
                completed = True
            finally:
                try:
                    with self._timed("caches"):
                        reset_client_caches()
                    self.token = self.reset(tenant)
                except BaseException as exc:
                    self.retire(f"{type(exc).__name__}: {exc}")
                    if not isinstance(exc, Exception):
                        raise  # KeyboardInterrupt, SystemExit: stop, the tenant is already retired
                    if completed:
                        if isinstance(exc, TenantLeakError):
                            raise
                        raise TenantLeakError(
                            f"tenant {tenant}: teardown failed with {type(exc).__name__}: {exc}") from exc
                    _log.warning("tenant_pool_teardown_failed_after_test_error",
                                 tenant=tenant, error=f"{type(exc).__name__}: {exc}")
                else:
                    self._last_owner = owner
        finally:
            self._leased = False

    def _census_at_start(self, tenant: str, token: str | None, owner: str) -> None:
        """Before the test body: the tenant holds no rows but the bearer it was handed. A row means a writer fired
        after the previous lease's teardown census; fail THIS setup naming the previous owner, and retire."""
        if not self.start_census:
            return
        try:
            with self._timed("start_census"):
                census(self.state, tenant, allow_token=token or "")
        except BaseException as exc:
            previous = self._last_owner or "(none recorded: a freshly minted tenant)"
            self.retire(f"lease-start census: {exc}")
            if not isinstance(exc, Exception):
                raise
            raise TenantLeakError(
                f"{exc}; found at the START of the lease for {owner or '(unnamed)'}, before its body ran; the previous "
                f"lease, held by {previous}, is the likely leaker (a writer that fired after its teardown census)"
            ) from exc

    def final_census(self) -> str | None:
        """The end-of-session verdict on the pool tenant: None when empty or never minted, else the reason."""
        if self.tenant is None:
            return None
        try:
            census(self.state, self.tenant, allow_token=self.token or "")
        except TenantLeakError as exc:
            return str(exc)
        return None

    def leaf_bloat(self) -> tuple[int, int, int] | None:
        """``(n_dead_tup, n_live_tup, bytes)`` summed over the pool tenant's ``nexus.chunks`` leaves, or None when
        there is no tenant or the read failed. Thousands of delete-only resets leave dead tuples a per-test drop never
        would; the end-of-session line reports them so the A/B can say whether it matters. (``pg_stat`` counters lag
        a flush interval behind, so the dead count is a floor.)"""
        if self.tenant is None:
            return None
        try:
            proc = psql(self.state, LEAF_BLOAT_SQL, tenant=self.tenant)
            dead, live, size = (int(x) for x in proc.stdout.split())
        except Exception:  # noqa: BLE001 — a failed read is "unknown", never a failed run
            return None
        return dead, live, size


# -- the process's pool ----------------------------------------------------------------------------------

_pool: TenantPool | None = None
_pool_lock = threading.Lock()
#: Entries into ``_engine_tenant_env`` this process saw, and how many took the pool vs. the per-test mint.
_entries = {"pooled": 0, "fresh": 0}


def get_pool(state: Mapping[str, Any]) -> TenantPool:
    """This process's pool, created on first use for the process's one substrate."""
    global _pool
    with _pool_lock:
        if _pool is None or _pool.state is not state:
            _pool = TenantPool(state)
        return _pool


def tenant_lease(state: Mapping[str, Any], request: pytest.FixtureRequest | None) -> contextlib.AbstractContextManager:
    """The ``(tenant, token)`` context a tenant fixture uses: the pool's lease when the flag is on and the test
    does not carry ``@pytest.mark.fresh_tenant``, else the per-test mint and drop."""
    from tests._engine_substrate import minted_test_tenant  # noqa: PLC0415

    opted_out = request is not None and request.node.get_closest_marker("fresh_tenant") is not None
    if enabled() and not opted_out:
        _entries["pooled"] += 1
        return get_pool(state).lease(owner=request.node.nodeid if request is not None else "")
    if enabled():
        _entries["fresh"] += 1
    return minted_test_tenant(state)


def _reset_for_tests() -> None:
    global _pool
    with _pool_lock:
        _pool = None
        _entries.update(pooled=0, fresh=0)


# -- plugin ---------------------------------------------------------------------------------------------


def worker_report() -> dict[str, Any]:
    pool = _pool
    verdict = pool.final_census() if pool is not None else None
    violations: list[str] = []
    if verdict is not None:
        violations.append(f"pid {os.getpid()}: end-of-session census: {verdict}")
    leases = pool.leases if pool is not None else 0
    bloat = pool.leaf_bloat() if pool is not None else None
    if _entries["pooled"] and leases < 1:
        violations.append(f"pid {os.getpid()}: {_entries['pooled']} tenant tests asked for the pool and no lease was taken")
    if _entries["pooled"] != leases:
        violations.append(f"pid {os.getpid()}: {_entries['pooled']} pooled entries but {leases} leases")
    return {
        "pid": os.getpid(),
        "pooled_entries": _entries["pooled"],
        "fresh_tenant_entries": _entries["fresh"],
        "leases": leases,
        "retired": pool.retired if pool is not None else 0,
        "mints": pool.mints if pool is not None else 0,
        "evicted": dict(pool.evicted) if pool is not None else {},
        "step_s": dict(pool.step_s) if pool is not None else {},
        "leaf_dead_tup": bloat[0] if bloat else 0,
        "leaf_live_tup": bloat[1] if bloat else 0,
        "leaf_bytes": bloat[2] if bloat else 0,
        "violations": violations,
    }


def merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"workers": len(reports), "leases": 0, "retired": 0, "mints": 0, "fresh_tenant_entries": 0,
                           "pooled_entries": 0, "evicted": {}, "step_s": {}, "violations": [],
                           "leaf_dead_tup": 0, "leaf_live_tup": 0, "leaf_bytes": 0}
    for r in reports:
        for key in ("leases", "retired", "mints", "fresh_tenant_entries", "pooled_entries", "leaf_dead_tup",
                    "leaf_live_tup", "leaf_bytes"):
            out[key] += r.get(key, 0)
        for name, n in r.get("evicted", {}).items():
            out["evicted"][name] = out["evicted"].get(name, 0) + n
        for name, sec in r.get("step_s", {}).items():
            out["step_s"][name] = out["step_s"].get(name, 0.0) + sec
        out["violations"].extend(r.get("violations", []))
    return out


def summary_line(merged: dict[str, Any]) -> str:
    leases = merged["leases"]
    steps = " ".join(
        f"{name}={1000 * sec / leases:.0f}ms" for name, sec in sorted(merged.get("step_s", {}).items())
    ) if leases else ""
    return (
        f"tenant pool: leases={merged['leases']} mints={merged['mints']} retired={merged['retired']} "
        f"fresh_tenant={merged['fresh_tenant_entries']} workers={merged['workers']} "
        f"leaf_dead_tup={merged['leaf_dead_tup']} leaf_live_tup={merged['leaf_live_tup']} leaf_bytes={merged['leaf_bytes']}"
        + (f" per_lease[{steps}]" if steps else "")
        + (f" VIOLATIONS={len(merged['violations'])}" if merged["violations"] else "")
    )


class _Plugin:
    def __init__(self) -> None:
        self.reported: list[dict[str, Any]] = []
        self.merged: dict[str, Any] | None = None

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
        if payload:
            self.reported.append(payload)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if session.config.option.collectonly:
            return
        workeroutput = getattr(session.config, "workeroutput", None)
        if workeroutput is not None:
            workeroutput[_WORKEROUTPUT_KEY] = worker_report()
            return
        if not session.config.pluginmanager.has_plugin("dsession"):
            self.reported.append(worker_report())
        self.merged = merge_reports(self.reported)
        if self.merged["violations"] and int(session.exitstatus) == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any, exitstatus: Any, config: pytest.Config) -> None:
        if hasattr(config, "workerinput") or self.merged is None:
            return
        terminalreporter.write_line(summary_line(self.merged))
        for v in self.merged["violations"]:
            terminalreporter.write_line(f"tenant pool VIOLATION: {v}")


def pytest_configure(config: pytest.Config) -> None:
    if not enabled():
        return
    plugin = _Plugin()
    config.stash[_STASH] = plugin
    config.pluginmanager.register(plugin, "nx-tenant-pool")


def pytest_unconfigure(config: pytest.Config) -> None:
    plugin = config.stash.get(_STASH, None)
    if plugin is not None:
        del config.stash[_STASH]
        config.pluginmanager.unregister(plugin)


__all__ = [
    "CENSUS_SQL", "ENV_FLAG", "LEAF_BLOAT_SQL", "MARKER", "MIN_CENSUS_RELATIONS", "REQUIRED_CENSUS_RELATIONS", "ROUTE_ENV", "TenantLeakError",
    "TenantLeaseBusyError", "TenantPool", "WIPE_SQL", "census", "enabled", "get_pool", "psql", "reset_client_caches", "tenant_lease", "wipe",
]
