import logging
import os

import pytest
from pathlib import Path

import structlog
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database


# NO _enable_t2_test_auto_migrate: the RDR-120 P3b auto-migrate default
# (``_DEFAULT_RUN_MIGRATIONS`` / ``NX_T2_AUTO_MIGRATE``) died with
# ``nexus/db/migrations.py`` in RDR-158 P4 Stage 4 (nexus-i711w).
# ``T2Database(run_migrations=...)`` is retained-and-ignored for signature
# stability; construction never migrates anything in any mode.


def _disable_aspect_worker_autostart() -> None:
    """Stop the aspect-extraction-enqueue hook from lazy-spawning the
    singleton polling worker during the unit suite.

    A ``store_put`` / index / MCP test that touches a supported collection
    fires ``aspect_extraction_enqueue_hook``, which (in production)
    auto-spawns the polling worker. The worker then gets stuck mid
    ``t2_index_write`` poll, so the autouse ``_reset_aspect_worker_singleton``
    teardown's ``stop()`` join waits its full 5s timeout — a fixed ~5s tax on
    every such test (≥140s across the suite). The worker is never asserted on
    by those tests, and leaving it unspawned also removes the leaked-singleton
    hazard (nexus-u0u8a) at its root. Worker-specific tests call
    ``ensure_worker_started()`` directly, which ignores this gate, or
    ``monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "1")`` to exercise the
    hook path. ``setdefault`` so an explicit opt-in set before import wins.
    """
    import os

    os.environ.setdefault("NX_ASPECT_WORKER_AUTOSTART", "0")


_disable_aspect_worker_autostart()

# RDR-155 P4b P0a': import at collection start so the engine substrate
# resolves PG binaries against the AMBIENT env (per-test fixtures patch
# HOME/NEXUS_CONFIG_DIR before the lazy first ensure_engine() call).
import tests._engine_substrate  # noqa: E402, F401


def pytest_configure(config):
    """Configure structlog level to match pytest's --log-level.

    Default run: WARNING level — quiet, no clutter.
    Validation run: pytest --log-level=DEBUG — full structlog output to stdout.

    Example:
        uv run pytest                          # quiet (WARNING)
        uv run pytest --log-level=DEBUG        # full debug output
    """
    try:
        level_str = (config.getoption("log_level") or "WARNING").upper()
    except (ValueError, AttributeError):
        level_str = "WARNING"
    level = getattr(logging, level_str, logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


# nexus-nifd: prefixes that the indexer's repo cache uses for
# pytest fixture-named test repos. Files at
# ``~/.config/nexus/<prefix>-*-<repo_hash>.cache`` matching one of
# these are evidence that a test bypassed the autouse
# ``_isolate_config_dir`` fixture (e.g. a subprocess that didn't
# inherit ``NEXUS_CONFIG_DIR``, or a test that explicitly
# ``monkeypatch.delenv("NEXUS_CONFIG_DIR")``). Update this list when
# adding a new fixture-named test repo.
_FIXTURE_CACHE_PREFIXES: tuple[str, ...] = (
    "nexus-rich0",
    "nexus-mini0",
    "code-repo",
    "prose-repo",
    "pdf-repo",
    "stage-b-repo",
    "sentinel-repo",
    "test-repo",
    "nx-shakeout-",
)


def _scan_fixture_cache_files() -> set[Path]:
    """Return the set of *.cache files in the REAL ~/.config/nexus/
    whose basename starts with a fixture-cache prefix. Empty when
    the directory doesn't exist.

    Uses Path.home() rather than ``nexus_config_dir()`` to bypass
    any test-time NEXUS_CONFIG_DIR override; the leak we're guarding
    against is precisely tests that hit the REAL config dir.
    """
    real_config = Path.home() / ".config" / "nexus"
    if not real_config.exists():
        return set()
    return {
        p for p in real_config.glob("*.cache")
        if p.name.startswith(_FIXTURE_CACHE_PREFIXES)
    }


_fixture_cache_baseline: set[Path] = set()


def _warn_if_service_jar_is_stale() -> None:
    """Say ONCE, at session start, that the service jar is stale (nexus-zryqm).

    The information already exists: ``jar_freshness_skip_reason`` is consulted
    per-test by the engine-substrate fixtures, which fail LOUD with a directive
    message. That is right for a targeted run and wrong for a full suite — it
    surfaces as ~73 identical errors THIRTEEN MINUTES IN, after which the whole
    run has to be discarded and repeated.

    That happened three times in one day (2026-07-25), twice after the operator
    had read a handoff note explicitly warning about it. A documented
    precondition that a human must remember is not a mechanism; this makes the
    same fact arrive at second 2 instead of minute 13.

    Deliberately a WARNING, not a hard stop: the stale jar only affects the
    engine-substrate tests, and someone iterating on unrelated Python must not
    be blocked by a Java artifact they never touched. The per-test fail-loud
    guard is unchanged and still authoritative.
    """
    try:
        from tests.db._service_fixture import jar_freshness_skip_reason
    except Exception:  # noqa: BLE001 — advisory only; never break collection
        return
    try:
        reason = jar_freshness_skip_reason()
    except Exception:  # noqa: BLE001 — advisory only
        return
    if not reason:
        return
    import sys as _sys

    banner = (
        "\n"
        "=" * 78 + "\n"
        f"SERVICE JAR STALE — engine-substrate tests will error: {reason}\n"
        "Rebuild BEFORE trusting this run, or ~73 errors will surface at the END:\n"
        "    mvn -f service/pom.xml package -DskipTests\n"
        "(nexus-zryqm: this notice exists because the same 13-minute run was\n"
        " discarded three times in one day for exactly this reason.)\n"
        + "=" * 78 + "\n"
    )
    print(banner, file=_sys.stderr)  # noqa: T201 — session banner, must be seen before the run


def pytest_sessionstart(session):
    """Snapshot fixture cache files in ~/.config/nexus/ at session
    start so ``pytest_sessionfinish`` can detect leaks introduced
    during the session (nexus-nifd).

    Also emits the stale-service-jar banner (nexus-zryqm) so a doomed
    engine-substrate run is visible immediately rather than 13 minutes later.
    """
    global _fixture_cache_baseline
    _fixture_cache_baseline = _scan_fixture_cache_files()
    _warn_if_service_jar_is_stale()


def pytest_sessionfinish(session, exitstatus):
    """nexus-nifd: fail the session when any new test-fixture cache
    file appears in the REAL ~/.config/nexus/ during the session.

    Background: 2026-05-08 prod shakeout found 1,707 leaked
    test-fixture cache files (~121.5 MB) accumulated over weeks.
    The autouse ``_isolate_config_dir`` fixture (PR #601 / nexus-
    mrmq) prevents future leakage for tests that USE it, but a
    test that bypasses the fixture or spawns a subprocess without
    propagating ``NEXUS_CONFIG_DIR`` could re-introduce the leak
    silently. This guard catches that class.

    Best-effort cleanup: any newly-leaked file is unlinked before
    the failure surfaces so the next run starts from a clean
    baseline. The session is still failed so the offending test
    is visible in CI.
    """
    after = _scan_fixture_cache_files()
    leaked = after - _fixture_cache_baseline
    if leaked:
        # Surface and clean up.
        leaked_sorted = sorted(leaked)
        for path in leaked_sorted:
            try:
                path.unlink()
            except OSError:
                pass
        names = ", ".join(p.name for p in leaked_sorted[:5])
        suffix = "" if len(leaked_sorted) <= 5 else f" (+{len(leaked_sorted) - 5} more)"
        session.exitstatus = 1
        print(
            f"\n\nFAIL: nexus-nifd cache-leak guard caught "
            f"{len(leaked_sorted)} fixture-cache file(s) leaked into "
            f"~/.config/nexus/: {names}{suffix}\n"
            f"  Cause: a test bypassed the autouse `_isolate_config_dir` "
            f"fixture or spawned a subprocess without inheriting "
            f"NEXUS_CONFIG_DIR.\n"
            f"  Cleanup: leaked files removed; failing the session.\n",
            flush=True,
        )

    _check_scenario_non_vacuity(session)
    _check_mandatory_pin_non_vacuity(session)


# ── `scenario` marker non-vacuity guard (test-suite-compression P2-reduced,
# nexus-test-cleanup 2026-08-05; redesigned same-day after substantive-critic
# reproduced it as VACUOUS under xdist — T2
# nexus/test-suite-compression-P2-reduced-critique) ─────────────────────────
#
# ROOT CAUSE of the xdist gap the first cut shipped with: it tracked
# selection/outcomes via its own ``pytest_collection_modifyitems`` /
# ``pytest_runtest_logreport`` hooks and enforced by mutating
# ``session.exitstatus`` in EVERY process's own ``pytest_sessionfinish`` —
# including xdist WORKER processes. Under ``-n0`` (no xdist) or
# ``--splits``/``--group`` (pytest-split; each shard is its own separate
# process, architecturally identical to ``-n0``) that works, because the
# process that ran the guard IS the process whose exit code becomes the
# real ``pytest`` exit code. Under ``-n N`` (xdist) it does not: the
# CONTROLLER process is the one whose exit status becomes the real exit
# code, and a WORKER's local ``session.exitstatus`` mutation is discarded
# once its results are reported back — so a scenario that unconditionally
# skips trips the guard at ``-n0`` but yields a silent green exit 0 at
# ``-n 2``, exactly the class of incident (silent skip read as pass) this
# guard exists to prevent, on this project's OWN documented dev-loop fast
# path (``uv run pytest -n auto``).
#
# FIX: enforce ONLY on the controller/serial side (never on an xdist
# worker — see ``_is_xdist_worker`` below), and derive BOTH the selected
# set and every outcome from the aggregated ``terminalreporter`` stats,
# never from ``session.items`` / a locally-tracked hook. Empirically
# verified (throwaway probe, ``-n 2``): the xdist CONTROLLER process does
# not run collection itself — ``session.items`` is EMPTY there, and its
# own ``pytest_collection_modifyitems`` never fires with a populated item
# list; only the WORKERS collect. What the controller does reliably have
# is every worker's ``TestReport`` replayed through the standard
# ``pytest_runtest_logreport`` hook as results come back (the same
# mechanism that powers xdist's live per-worker progress output), which
# is exactly what populates ``terminalreporter.stats`` — the reconstruction
# below is built entirely from that, in every mode (serial, ``-n N``,
# ``--splits``/``--group`` — a pytest-split shard's own process populates
# ``terminalreporter.stats`` from its own locally-run tests the same way a
# serial run does). The FIRST cut filtered on ``report.keywords.get
# ("scenario")`` — the same signal ``-m`` selection reads — reasoning it was
# "a dict of every marker on the item". That reasoning had a bug, found and
# fixed the same day; see below.
#
# ``pytest_collection_modifyitems`` below stamps every item's own
# ``get_closest_marker("scenario")`` verdict into ``user_properties`` — a
# live, non-comment ``get_closest_marker("scenario")`` consumer, which is
# what makes ``tests/test_marker_selection_coverage.py`` treat ``scenario``
# as wired (path 2: programmatic consumption) even though it runs in the
# default loop with no explicit ``-m scenario`` invocation anywhere.
#
# FALSE-POSITIVE FOUND 2026-08-05 (nexus-test-cleanup CI red, test-lint
# job): the first cut of this guard filtered
# ``terminalreporter.stats`` reports on ``report.keywords.get("scenario")``.
# ``TestReport.keywords`` is NOT "markers on the item" — empirically it is
# ``dict(item.keywords)``, which ALSO contains a truthy entry for every
# token pytest derives from the node's own name, including each
# ``@pytest.mark.parametrize`` id. ``tests/test_marker_selection_coverage.py``
# parametrizes ``test_marker_is_wired_to_a_real_invocation`` over every
# declared marker NAME — one of which is the literal string ``"scenario"``
# (since ``scenario`` is itself a declared marker) — so its
# ``[scenario]`` id collides with the real ``@pytest.mark.scenario`` keyword
# with NO actual scenario mark applied. In ``test-lint`` (no service jar,
# no ``NX_T2_SUBSTRATE_EXPECTED``) every test — including that one — is
# legitimately skipped by the autouse ``_pin_t2_substrate`` fixture, and
# the false-positive keyword match read that one unrelated lint-test skip
# as "a scenario journey silently degraded", tripping the budget=0 guard
# on a job that runs zero real ``scenario`` tests (deselected by ``-m
# lint``). Reproduced directly: a throwaway
# ``@pytest.mark.parametrize("marker", ["scenario"])`` test shows
# ``"scenario" in item.keywords`` True with ``item.get_closest_marker
# ("scenario")`` False.
#
# FIX: stamp the real verdict onto ``item.user_properties`` at collection
# time (a plain list of ``(name, value)`` tuples that IS part of the
# standard ``TestReport`` — including across an xdist worker -> controller
# relay) instead of relying on ``report.keywords``, which cannot
# distinguish an applied mark from an accidental name/parametrize-id
# substring. This runs during COLLECTION, which happens in every process
# that collects tests — the controller included on a serial/pytest-split
# run, workers only under true ``-n`` xdist — so it is populated in every
# execution mode this guard needs to reason about.
_SCENARIO_PROPERTY = "nx_scenario_marked"

# ── `mandatory_regression_pin` marker non-vacuity guard (nexus-moht0
# follow-up / nexus-93j33, 2026-08-12) ──────────────────────────────────────
#
# Same shape as the `scenario` guard above, generalized rather than
# duplicated (see `_outcomes_from_terminalreporter` / `_check_marker_non_
# vacuity` below) so the exact false-positive class the scenario guard's
# history warns about (filtering `report.keywords`, which also carries a
# truthy entry for every parametrize-id/name substring — see the FALSE-
# POSITIVE FOUND 2026-08-05 comment above) cannot recur for this marker
# either: BOTH guards stamp their verdict via `get_closest_marker()` into
# `item.user_properties` at collection time and read that back, never
# `report.keywords`.
#
# WHY THIS EXISTS: substantive-critic review of the nexus-f9z84/jvhsw/cqquo/
# hs4xl vacuous-gate fixes found the fixes' OWN "mandatory regression pin"
# tests (test_v7_6_1_merge_commit_has_real_green_evidence,
# test_required_check_contexts_matches_live_branch_protection,
# test_v7_6_1_source_ancestry_regression_is_red) marked `integration` to
# dodge the unit suite -- correctly -- but a marker only moves a test
# somewhere it CAN run; nothing proved it DID. All three need an
# environment `ci.yml`'s `test` job deliberately does not provide (full git
# history for tags, GITHUB_TOKEN for the live API — nexus-dhs30's `fetch-
# tags: true` was tried and rejected there already), so wherever they
# actually get invoked with `-m integration`, this guard is what turns "all
# three silently skipped" into a failed run instead of a green one.
_MANDATORY_PIN_PROPERTY = "nx_mandatory_pin_marked"


# ── partial-view guard for session.items-based censuses (nexus-vdti6,
# 2026-08-06) ──────────────────────────────────────────────────────────────
#
# WHY: test_mode_declarations_are_explicit.py's census reads
# request.session.items to scan every collected test for an undeclared
# voyage-mode reference. CI's real PR-gating `test` job runs
# `pytest tests/ --splits 4 --group N` (pytest-split); pytest_split/
# plugin.py:168 does `items[:] = group.selected` inside its OWN
# pytest_collection_modifyitems -- the identical session.items-mutation
# mechanism `-m`/`-k` deselection uses (the same mechanism nexus-8x4le's
# `-m lint` blind spot exploited). Under that real invocation the census
# only ever sees its own shard (~20-25% of the corpus): enforcement was
# shard-placement luck, not real coverage. This section makes that state
# structurally impossible to mistake for enforcement -- ANY
# session.items-based census can call partial_session_view_reason(request)
# and, on a non-None return, refuse (skip -- "cannot determine anything
# either way", never a silent pass and never a false FAIL) instead of
# scanning a proven-partial view.
_raw_collected_count = 0


def pytest_itemcollected(item: pytest.Item) -> None:
    """Count every item as pytest collects it, strictly BEFORE any
    ``pytest_collection_modifyitems`` hook runs.

    ``-m``/``-k`` deselection (``_pytest/mark/__init__.py``, itself
    ``@hookimpl(tryfirst=True)``) and pytest-split's shard selection
    (``@hookimpl(trylast=True)``) are both ``pytest_collection_
    modifyitems`` hookimpls; a conftest-level hook of the SAME name would
    have no guaranteed order against either. ``pytest_itemcollected`` fires
    per-item during the collection phase itself, before
    ``pytest_collection_modifyitems`` is invoked at all, so this count
    sidesteps hook-ordering entirely rather than depending on it.

    A path-scoped invocation (e.g. ``pytest tests/upgrade/``) is already
    reflected here: only items under that path get collected in the first
    place, so the baseline matches the invocation's own intended scope, not
    the whole repo -- a narrow path-scoped run does not, by itself, look
    like a partial view below.
    """
    global _raw_collected_count
    _raw_collected_count += 1


# Below this ratio of (session.items after all deselection) / (raw
# collected count), a session.items-based census cannot see enough of the
# corpus to mean anything. 50% is generous headroom over both known shrink
# mechanisms: `-m lint` collapses to ~6% (803/13101) and a pytest-split
# shard to ~20-25% (2796/13101) -- either trips this floor by a wide
# margin. The normal default-loop reduction from
# `-m 'not integration and not slow and not lint'` alone measures ~90%
# (11733/13101, reproduced live 2026-08-06) and stays comfortably above it.
_SESSION_VIEW_SANITY_FLOOR = 0.5


def partial_session_view_reason(request: pytest.FixtureRequest) -> str | None:
    """Return a reason if *request.session.items* is a proven-partial view,
    or ``None`` if it is trustworthy enough for a session.items-based
    census to scan.

    Two signals, checked in order:

    1. pytest-split shard mode. ``PytestSplitPlugin`` registers iff BOTH
       ``--splits`` and ``--group`` are set (``pytest_split.plugin.
       pytest_configure``), and its ``pytest_collection_modifyitems`` does
       ``items[:] = group.selected`` -- the exact mechanism nexus-vdti6 was
       filed for. Deterministic; no count heuristics needed.
    2. A generic floor against the raw pre-deselection count from
       ``pytest_itemcollected`` above, guarding any OTHER future
       session.items-shrinking mechanism ("the same class could affect any
       future session.items-based census", nexus-vdti6's own framing).
       Skipped entirely when ``-k`` was passed: a developer's deliberate
       keyword narrowing that happens to still select a session.items-based
       census is a normal targeted run, not shard-blindness reappearing
       under a new name, and must not false-alarm.

    A test whose OWN selection was deselected (``-m lint``, an unrelated
    ``-k``) never calls this at all -- it simply does not run, which is the
    correct, unproblematic case; this function only ever fires for a
    census that DID run.
    """
    config = request.config
    if config.getoption("splits", default=None) and config.getoption(
        "group", default=None
    ):
        return (
            f"pytest-split active (--splits={config.getoption('splits')} "
            f"--group={config.getoption('group')}): session.items is one "
            "shard of the corpus, not the whole thing (nexus-vdti6) -- a "
            "session.items-based census cannot enforce anything meaningful "
            "here. Enforcement runs in the dedicated non-sharded CI job "
            "instead (see .github/workflows/ci.yml, NX_CENSUS_ONLY_JOB)."
        )
    if config.getoption("keyword", default=""):
        return None
    raw = _raw_collected_count
    final = len(request.session.items)
    if raw <= 0:
        return None
    ratio = final / raw
    if ratio < _SESSION_VIEW_SANITY_FLOOR:
        return (
            f"session.items shrank to {final}/{raw} items ({ratio:.0%}, "
            f"floor {_SESSION_VIEW_SANITY_FLOOR:.0%}) by some mechanism "
            "other than --splits/--group or -k -- refusing to scan a "
            "proven-partial view rather than pass vacuously."
        )
    return None


# Every session.items-based census test whose partial-view guard must be
# evaluated PRE-FIXTURE (see pytest_runtest_setup below), keyed by full
# nodeid. Extend alongside `_CENSUS_ONLY_ALLOWED_MODULES` for a future
# sibling census -- add the SPECIFIC test nodeid(s) that call
# `partial_session_view_reason`, not the whole module (a module's ratchet/
# liveness siblings don't read session.items and must never be skipped by
# this hook).
_SESSION_ITEMS_CENSUS_TEST_NODEIDS: frozenset[str] = frozenset({
    "tests/test_mode_declarations_are_explicit.py"
    "::test_mode_declarations_are_explicit",
})


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> None:
    """Stash each phase's TestReport on the item (``rep_setup`` /
    ``rep_call`` / ``rep_teardown`` -- the standard pytest idiom, e.g. the
    ``pytest-html``/``pytest-rerunfailures`` "did this test fail" pattern).

    Zero-cost on a green run: a plain attribute set per phase, no I/O, no
    behavior change to the report itself. Added so any fixture in the suite
    can check ``request.node.rep_call.failed`` in its own teardown without
    each one re-registering this hook -- the FIRST consumer is
    ``tests/test_scenario_journeys.py``'s ``_engine_log_on_failure``
    fixture (substantive-critic Q1: the engine's nexus-c8hl7 refusal WARN
    otherwise dies with the substrate's mkdtemp'd pgdata at session
    teardown, unread).
    """
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Evaluate the partial-view guard BEFORE any fixture -- including the
    autouse engine-substrate fixture -- is resolved for a registered
    session.items-based census test.

    WHY THIS MUST RUN PRE-FIXTURE, NOT INSIDE THE TEST BODY (nexus-vdti6,
    CI red 2026-08-06, develop run 31061260804): the first cut called
    `partial_session_view_reason(request)` as the FIRST LINE of
    `test_mode_declarations_are_explicit`'s own body. By the time a test's
    body executes, `_pytest/runner.py`'s `pytest_runtest_setup` has ALREADY
    called `item.setup()`, which resolves every fixture the item depends
    on -- including the autouse `_pin_t2_substrate` fixture, which boots
    the engine substrate for EVERY test unless `NX_TEST_T2_SUBSTRATE=none`.
    Locally this was invisible (the box happily absorbed a second engine
    boot from the subprocess probe's nested pytest invocation); under a
    REAL `--splits`/`--group` CI shard, the nested invocation's engine boot
    collided with the outer shard job's already-bound engine ("service did
    not bind port 42279") and FAILED loudly during fixture setup, before
    the in-body skip ever ran -- the test errored instead of skipping
    cleanly. That is backwards twice over: it breaks CI, and even where it
    doesn't, a shrunk-view census should be CHEAP (no engine boot at all),
    not merely non-fatal -- paying a ~10s engine boot per shard just to
    immediately skip defeats the "structural honesty AND cheap" point of
    nexus-vdti6's guard.

    This mirrors `_pytest/skipping.py`'s own approach exactly:
    `@hookimpl(tryfirst=True)` on `pytest_runtest_setup` runs before
    `_pytest/runner.py`'s `pytest_runtest_setup` (the one that calls
    `item.setup()`), so a `pytest.skip()` raised here pre-empts fixture
    resolution entirely -- the SAME mechanism `NX_CENSUS_ONLY_JOB`'s
    skip-marking already relies on (see `_skip_mark_everything_but_census`
    below), just applied per-invocation via a runtime hook instead of a
    collection-time marker (this hook must run on every invocation --
    including the sharded `test` matrix and a plain local `-n auto` run --
    not just the one `NX_CENSUS_ONLY_JOB` job).

    Scoped to `_SESSION_ITEMS_CENSUS_TEST_NODEIDS` so it can never
    misfire on an unrelated test -- `item` and `item.session` expose the
    same `.config` / `.session.items` shape `partial_session_view_reason`
    already accepts, so no adapter is needed.
    """
    if item.nodeid not in _SESSION_ITEMS_CENSUS_TEST_NODEIDS:
        return
    reason = partial_session_view_reason(item)
    if reason is not None:
        pytest.skip(reason)


# ── dedicated non-sharded CI job support (nexus-vdti6 candidate 1) ─────────
#
# The new `pytest (mode-declarations census)` job needs request.session.
# items to be the REAL full default-loop corpus (so the guard above finds
# it trustworthy), but must not pay to EXECUTE ~11.7k tests' fixtures
# (engine substrate, PG, service jar -- the ~25min the sharded `test`
# matrix already pays 4x for). Narrowing via `-k`/`-m` is not an option --
# that shrinks session.items through the exact items[:] = selected
# mechanism this whole guard exists to catch. SKIP-marking instead of
# deselecting keeps every item in session.items (collection is untouched)
# while making every non-census item's setup an near-instant `Skipped`
# with NO fixture resolution: `_pytest/skipping.py`'s skip check is
# `@hookimpl(tryfirst=True)` on `pytest_runtest_setup` and raises before
# `_pytest/runner.py`'s own `pytest_runtest_setup` (the one that calls
# `item.setup()` and instantiates fixtures) ever runs -- verified against
# the installed pytest source, not assumed.
_NX_CENSUS_ONLY_ENV = "NX_CENSUS_ONLY_JOB"

# Every module this dedicated job exists to run for real. Extend this
# alongside any future session.items-based census (see tests/AGENTS.md's
# session.items rule) so the same job covers every "same-pattern sibling"
# without spinning up a second dedicated job per census.
_CENSUS_ONLY_ALLOWED_MODULES: frozenset[str] = frozenset({
    "test_mode_declarations_are_explicit.py",
})


def _skip_mark_everything_but_census(items) -> None:
    for item in items:
        module_name = item.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
        if module_name in _CENSUS_ONLY_ALLOWED_MODULES:
            continue
        item.add_marker(
            pytest.mark.skip(
                reason=(
                    f"{_NX_CENSUS_ONLY_ENV}: this job exists solely to give "
                    "session.items-based censuses a real, unsharded view of "
                    "the full corpus; every non-census test is skip-marked "
                    "(not deselected -- session.items stays the full "
                    "collected count) rather than executed."
                )
            )
        )


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if item.get_closest_marker("scenario") is not None:
            item.user_properties.append((_SCENARIO_PROPERTY, True))
        if item.get_closest_marker("mandatory_regression_pin") is not None:
            item.user_properties.append((_MANDATORY_PIN_PROPERTY, True))

    if os.environ.get(_NX_CENSUS_ONLY_ENV):
        _skip_mark_everything_but_census(items)


def _is_xdist_worker(session) -> bool:
    """True when this process is an xdist WORKER subprocess.

    xdist sets ``config.workerinput`` only on worker-side configs
    (``xdist.workermanage.WorkerController`` / ``xdist.remote``); it is
    absent on the controller and on a plain serial (no ``-n``) run. Both
    of those non-worker cases are exactly where enforcement belongs — see
    the module comment above.
    """
    return hasattr(session.config, "workerinput")


def _outcomes_from_terminalreporter(terminalreporter, property_name: str) -> dict[str, str]:
    """Derive final per-test outcomes for every test stamped with
    *property_name* in ``item.user_properties``, keyed by nodeid.

    Generalized out of the original `scenario`-only implementation
    (nexus-93j33) so the `mandatory_regression_pin` guard below reuses the
    exact same phase-precedence / anti-false-positive logic instead of a
    hand-copied duplicate that could drift.

    Built entirely from ``terminalreporter.stats`` (see the module
    comment above for why: it is the only selection/outcome signal
    reliably present on an xdist controller). Mirrors the phase
    precedence a hook-based tracker would use: a test that skips does so
    at ``setup`` and never gets a ``call`` report, so ``skipped`` is taken
    from whichever phase carries it; a ``teardown`` failure after a
    passing ``call`` must not read as a clean pass.

    Filters on ``report.user_properties`` (stamped by
    ``pytest_collection_modifyitems`` above), NOT ``report.keywords`` —
    the latter also carries a truthy entry for every token pytest derives
    from the item's own name/parametrize id, which false-positived on
    ``test_marker_is_wired_to_a_real_invocation[scenario]`` (see the
    module comment above for the reproduction).
    """
    outcomes: dict[str, str] = {}
    for reports in terminalreporter.stats.values():
        for report in reports:
            if not dict(getattr(report, "user_properties", ())).get(property_name):
                continue
            nodeid = getattr(report, "nodeid", None)
            if nodeid is None:
                continue
            if getattr(report, "skipped", False):
                outcomes.setdefault(nodeid, "skipped")
            elif getattr(report, "when", None) == "call":
                if report.failed:
                    outcomes[nodeid] = "failed"
                elif outcomes.get(nodeid) != "failed":
                    # stats-category iteration order is not phase order: a
                    # passing call report must never downgrade a teardown
                    # failure already recorded for this nodeid.
                    outcomes[nodeid] = "passed"
            elif getattr(report, "when", None) == "teardown" and report.failed:
                outcomes[nodeid] = "failed"
    return outcomes


def _scenario_outcomes_from_terminalreporter(terminalreporter) -> dict[str, str]:
    return _outcomes_from_terminalreporter(terminalreporter, _SCENARIO_PROPERTY)


def _check_marker_non_vacuity(
    session,
    *,
    property_name: str,
    marker_label: str,
    env_var: str,
    default_budget: int,
    explanation: str,
    override_note: str,
) -> None:
    """Fail the session loudly if too many *marker_label*-marked tests
    skipped — never silently skip-pass (repo convention: a gate that
    skip-passes when its dependency is absent must carry a max-skip /
    non-vacuity assert).

    No-ops on an xdist WORKER (enforcement happens once, on the
    controller/serial process whose exit status is the real process exit
    code — see the module comment above). Inert when zero marked tests
    reported an outcome in THIS process — a shard, filtered, or
    default-addopts run that never even SELECTED any (e.g. `integration`-
    marked tests under the default `-m 'not integration...'` addopts) is
    fine; this guard only has teeth once such a test is actually
    collected and run. Otherwise at most *default_budget* (overridable via
    *env_var*) of them may skip; more than that fails the session even if
    every individual test reported a clean ``pytest.skip()``.
    """
    if _is_xdist_worker(session):
        return
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is None:
        # No terminal reporter registered (e.g. -p no:terminalreporter) —
        # nothing to aggregate outcomes from; nothing this guard can check.
        return
    outcomes = _outcomes_from_terminalreporter(terminalreporter, property_name)
    if not outcomes:
        return
    # Honest denominator (code-review-expert, 2026-08-05): "ran" is every
    # marked nodeid with a logged outcome in this aggregate — never a
    # separately-tracked "selected" count that could silently overstate M
    # under a shard or partial selection.
    ran = sorted(outcomes)
    skipped = sorted(nid for nid in ran if outcomes[nid] == "skipped")
    budget = int(os.environ.get(env_var, str(default_budget)))
    if len(skipped) <= budget:
        return
    session.exitstatus = 1
    names = ", ".join(Path(n).name for n in skipped[:5])
    suffix = "" if len(skipped) <= 5 else f" (+{len(skipped) - 5} more)"
    print(
        f"\n\nFAIL: {marker_label} non-vacuity guard — {len(skipped)}/{len(ran)} "
        f"`{marker_label}`-marked test(s) with a logged outcome SKIPPED "
        f"(budget={budget}): {names}{suffix}\n"
        f"  {explanation}\n"
        f"  Override ({override_note}): {env_var}=<n>.\n",
        flush=True,
    )


def _check_scenario_non_vacuity(session) -> None:
    _check_marker_non_vacuity(
        session,
        property_name=_SCENARIO_PROPERTY,
        marker_label="scenario",
        env_var="NX_SCENARIO_SKIP_BUDGET",
        default_budget=0,
        explanation=(
            "A `scenario` test proves a real cross-verb journey against the "
            "session's engine substrate (tests/conftest.py::t2_service_env). A "
            "skip here usually means that substrate silently degraded (a "
            "stale/absent service jar) rather than a scenario genuinely being "
            "inapplicable — see tests/test_scenario_journeys.py."
        ),
        override_note="rare — e.g. a deliberately substrate-less shard",
    )


def _check_mandatory_pin_non_vacuity(session) -> None:
    """nexus-93j33: the `mandatory_regression_pin` sibling of the guard
    above. These tests (nexus-f9z84/jvhsw/hs4xl's "prove the gate fires
    against a deliberately broken input" regression pins) are ALSO
    `integration`-marked, so they are invisible to this guard under the
    default addopts run (correct — see `_check_marker_non_vacuity`'s
    "Inert when zero..." doc) and only have teeth in an invocation that
    actually selects `-m integration` and includes their modules. That is
    precisely the gap review found: a marker alone does not prove the
    pin ran, only that it CAN. This is what proves it did.
    """
    _check_marker_non_vacuity(
        session,
        property_name=_MANDATORY_PIN_PROPERTY,
        marker_label="mandatory_regression_pin",
        env_var="NX_MANDATORY_PIN_SKIP_BUDGET",
        default_budget=0,
        explanation=(
            "A `mandatory_regression_pin` test is a release/CI-gate "
            "regression test (nexus-moht0 vacuous-gate class) whose bead "
            "REQUIRES it to exercise a real external dependency (live git "
            "tags, an authenticated GitHub API call) rather than a mock. A "
            "skip here means the pin never actually proved anything this "
            "run — usually a missing GITHUB_TOKEN / unauthenticated `gh`, "
            "or a shallow checkout without the tags it needs (see each "
            "test's own skip reason)."
        ),
        override_note="rare — e.g. a deliberately token-less/tag-less integration run",
    )


@pytest.fixture(autouse=True)
def _restore_structlog_after_test():
    """Save and restore structlog config around every test so any test
    that calls ``structlog.configure(...)`` (directly or via
    ``nexus.logging_setup.configure_logging``) does not leak its
    config to downstream tests.

    Background: tests that swap ``logger_factory`` from the default
    ``PrintLoggerFactory`` to ``LoggerFactory(stdlib)`` reroute every
    structlog event from stderr to stdlib logging. ``capsys``-based
    assertions in unrelated tests then read empty strings while the
    event sits in caplog. The originally-affected test was
    ``test_plan_audit_logs_warning_on_clamp``, which fails when run
    after any test that pollutes structlog. Solving it per-file via
    individual autouse fixtures drifted; a global one is cheap and
    closes the door for new tests too.
    """
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture(autouse=True)
def _isolate_claude_code_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the ambient ``CLAUDE_CODE_SESSION_ID`` for every test.

    nexus-36q84: :func:`nexus.session.resolve_active_session_id` gained a
    new tier that reads ``CLAUDE_CODE_SESSION_ID`` (the harness-provided
    per-process env var Claude Code sets natively) between ``NX_SESSION_ID``
    and the ``current_session`` flat-file fallback. Because the unit suite
    itself typically runs *inside* a live Claude Code session (via the Bash
    tool), the real conversation's ``CLAUDE_CODE_SESSION_ID`` is present in
    ``os.environ`` for every subprocess/test-process — exactly the ambient
    pollution class this fixture family exists to close (see
    ``_isolate_t1_sessions`` above). Without
    this, any test exercising the flat-file or ``None`` fallback tiers of
    ``resolve_active_session_id`` would silently resolve to the real
    session id instead of the fixture/file value it asserts against.

    Tests that want to exercise the new tier explicitly
    ``monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ...)`` inside the test
    body, which overrides this fixture's ``delenv`` (later calls on the
    same ``monkeypatch`` win — same pattern documented on
    ``_isolate_config_dir``).
    """
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture(autouse=True)
def _isolate_t1_sessions(
    monkeypatch: pytest.MonkeyPatch, _pin_t2_substrate: None
) -> None:
    """Give every test its own minted, PG-backed T1 session (nexus-4lkmz).

    RDR-105 P4 (nexus-jnx7) collapsed T1 discovery to a single
    fail-loud gate. Through RDR-155 P4b, tests that needed a working T1
    without the real service opted in via ``NX_T1_ISOLATED=1`` (Path C),
    which this autouse fixture set process-wide so the suite got the
    process-scoped ``InMemoryVectorClient`` singleton by default.

    nexus-4lkmz (Hal determination 2026-07-28: "T1 exists in PG only")
    deletes that leg outright — setting ``NX_T1_ISOLATED`` now hard-fails
    (:class:`nexus.db.t1.T1IsolatedLegRetiredError`). The migration this
    fixture follows is the one the bead calls out explicitly: the
    minted-session PG-backed path via the engine substrate, not a new
    opt-out. It depends on ``_pin_t2_substrate`` (a real fixture
    parameter, not merely declaration order, so pytest resolves it
    FIRST regardless of textual position in this file) so the SAME
    session-scoped hermetic PG + service JAR that already backs every
    test's T2 storage also mints this test's T1 session — one HTTP round
    trip via :func:`nexus.db.t1.mint_t1_session_token`, injected as
    ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` so ``get_t1_database()``'s
    ``USE_INHERITED`` tier picks it up directly (no lease lookup, no
    CLI-dedicated mint).

    ``NX_TEST_T2_SUBSTRATE=none`` (a test that declared it needs no T2
    store at all — endpoint resolution, env scrubbing, T1's OWN
    fail-loud/isolation-retirement paths) provisions nothing here either:
    T1 rides the same substrate T2 does, so there is nothing to mint
    against. Those tests set up whatever T1 env they need directly.

    Tests that want a different T1 identity (a specific session id, an
    inherited/leased token, a fail-loud raise) override the env inside
    the test body — the same "later ``monkeypatch`` call on the same
    fixture instance wins" contract documented on ``_isolate_config_dir``.
    """
    if os.environ.get("NX_TEST_T2_SUBSTRATE") == "none":
        return

    from uuid import uuid4

    from nexus.db.t1 import mint_t1_session_token

    session_id = str(uuid4())
    minted = mint_t1_session_token(session_id, context="test isolation mint")
    monkeypatch.setenv("NX_T1_SESSION", minted["session_token"])
    monkeypatch.setenv("NX_T1_SESSION_ID", session_id)


@pytest.fixture(autouse=True)
def _pin_mineru_autostart_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite-wide MinerU autostart kill-switch (nexus-1qdb9).

    ensure_mineru_running() spawns a REAL mineru-api process on demand;
    an unpatched unit test that wanders into the PDF extractor's server
    check must never do that (2026-07-14: one suite run left four stray
    servers). Lifecycle tests that exercise the spawn path re-enable via
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1") + patched spawn core.
    """
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "0")


@pytest.fixture
def t2_service_env(request: pytest.FixtureRequest,
                   monkeypatch: pytest.MonkeyPatch) -> str:
    """Engine-backed T2 substrate env for one test (RDR-155 P4b P0a', D-A).

    Boots the session-scoped hermetic PG + service JAR on first use
    (tests/_engine_substrate.py, memoized) and points this test's env at
    it with a freshly MINTED tenant + tenant-bound token — the engine
    binds tenant to the BEARER server-side (AuthFilter Decision 1; the
    X-Nexus-Tenant header is ignored), so per-test isolation is a
    per-test token. Tests never share or clean up state. Returns the
    tenant name.

    The suite default: ``_pin_t2_substrate`` pulls this fixture in for every
    test. Still requestable directly by tests that want the tenant name.
    """
    from tests._engine_substrate import ensure_engine, mint_test_tenant
    from tests.db._service_fixture import jar_freshness_skip_reason

    # CI leg (RDR-155 P4b P0a' registered question, now due): the Python
    # CI job does not build the service JAR, so engine-substrate tests
    # SKIP there — with a non-vacuity backstop: once CI provisions the
    # JAR it sets NX_T2_SUBSTRATE_EXPECTED=1, after which an absent JAR
    # FAILS loudly again (the skip can never silently become permanent).
    # Provisioning work: bead nexus-CI-substrate (see g37fr).
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and not os.environ.get("NX_T2_SUBSTRATE_EXPECTED")
        and jar_freshness_skip_reason() is not None
    ):
        pytest.skip("engine substrate: service JAR not provisioned on CI "
                    "(tracked; NX_T2_SUBSTRATE_EXPECTED=1 re-arms fail-loud)")

    state = ensure_engine()
    tenant, token = mint_test_tenant(state)
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    monkeypatch.setenv("NX_SERVICE_URL", state["base_url"])
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    # ORTHOGONALITY PIN (nexus-aqbrk): this fixture selects a T2 SUBSTRATE.
    # It must not also change the install's cloud/local POSTURE — a different
    # axis entirely.
    #
    # ``NX_SERVICE_URL`` is overloaded. The T2 Http*Stores need it to find the
    # engine, but ``config.is_local_mode()`` also reads ``service_url`` as the
    # "this is a managed/cloud install" signal (nexus-3k43p, so a greenfield
    # managed user is not mis-detected as local). Setting it therefore flips
    # EVERY test in the suite from local to cloud posture as a side effect of
    # choosing where T2 rows live. The sqlite arm has no service_url, so it is
    # local — meaning the two arms were not comparing like with like.
    #
    # Measured, not assumed: tests/test_doc_indexer.py failed 12 on the engine
    # arm, 8 of them ``CredentialsMissingError: cannot index in cloud mode
    # without voyage_api_key``. Re-running with NX_LOCAL=1 took it to 8 — the
    # 4 mode-posture failures are a pure artifact of the substrate pin, and the
    # remainder are genuine catalog work.
    #
    # NX_LOCAL=1 restores the suite's default posture. Tests that WANT cloud
    # posture use the ``cloud_mode`` fixture, which setenvs NX_LOCAL=0 and
    # still wins: non-autouse fixtures resolve AFTER autouse ones, so its
    # setenv lands later on the same monkeypatch — the same ordering contract
    # documented on ``_isolate_service_endpoint_env`` below.
    monkeypatch.setenv("NX_LOCAL", "1")
    return tenant


@pytest.fixture(autouse=True)
def _isolate_service_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient service-endpoint env from every unit test (nexus-dvom6).

    Same ambient-pollution class as ``_isolate_config_dir`` /
    ``_isolate_t1_sessions`` / the ``CLAUDE_CODE_SESSION_ID`` scrub above, and
    the missing half of ``_pin_t2_substrate``'s stated intent
    ("independent of ambient service/lease state"): that fixture pins the
    BACKEND, but a developer shell that has sourced the managed-service
    credentials still leaks the ENDPOINT into every test.

    ``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT`` / ``NX_SERVICE_TOKEN`` are tier 1
    of :mod:`nexus.db.service_endpoint`'s resolution order, and
    ``NX_SERVICE_URL`` is the ``service_url`` credential override
    (``config.CREDENTIALS``). With any of them present, tests that assert on
    the "nothing is resolvable" failure modes instead resolve a real endpoint:
    ``test_missing_port_raises`` gets the "service_url is set but no token"
    error rather than the ``NX_SERVICE_PORT`` one it matches on, and the
    om64x lease-recovery tests never reach the lease tier they exist to
    exercise, because env-first already won.

    This is not a hypothetical: sourcing ``~/.config/nexus/activate.sh`` is the
    documented way to get a working token, and ``nx doctor``'s 401 advice
    (nexus-srt1m) sends operators straight to it. So before this fixture, the
    more correctly a developer configured their shell, the more of the suite
    failed -- and it failed as though the checked-out branch had broken
    something.

    ORDERING: must be defined BEFORE ``_pin_t2_substrate``. Autouse
    function-scoped fixtures run in definition order, and that fixture may pull
    in ``t2_service_env`` (via ``getfixturevalue``), which ``setenv``s
    ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` for the engine substrate. Tests
    requesting ``t2_service_env`` directly are also safe: non-autouse fixtures
    resolve after autouse ones. Either way the explicit ``setenv`` lands after
    this ``delenv`` and wins -- the same "later call on the same monkeypatch
    wins" contract documented on ``_isolate_config_dir``.
    """
    for var in (
        "NX_SERVICE_URL",
        "NX_SERVICE_TOKEN",
        "NX_SERVICE_HOST",
        "NX_SERVICE_PORT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _pin_t2_substrate(request: pytest.FixtureRequest) -> None:
    """Route every test to the session's T2 substrate — the ENGINE.

    Every test gets the session PG+JAR with a freshly minted tenant, exactly
    what the ``t2_service_env`` opt-in fixture provides. This is what the
    product actually ships on; ``storage_backend_for`` has defaulted to
    ``service`` since the T2 cutover, so the suite agrees with the shipping
    default instead of contradicting it.

    THE SQLITE LEG IS GONE (nexus-i711w Stage 1b, 2026-07-28). Until now
    ``NX_TEST_T2_SUBSTRATE=sqlite`` opted out to the local SQLite stores, and
    ``engine_substrate_selected()`` was the single lever every dies-roster
    ``skipif`` read. Both retire here, with the stores themselves: a predicate
    with one reachable value is not a choice, and 69 ``skipif`` markers reading
    a constant are worse than no marker at all.

    ``=sqlite`` now RAISES rather than resolving to the engine. It is the one
    value a stale shell can still be carrying — the escape hatch was
    documented, so someone bisecting an engine-side regression against "the old
    baseline" will type it again. Silently handing them the engine would give a
    green run that did not test what they believe it tested, which is the
    silent-fallback class the project bans outright.

    HISTORY, because the old body's rationale is still worth knowing: this
    fixture used to pin SQLite so a bare ``T2Database(path)`` would not
    construct Http* stores and try to reach the nexus-service — which unit
    tests neither ran nor wanted. That kept ~116 T2Database-constructing tests
    deterministic and independent of ambient service/lease state (a dev box
    with the supervisor running would otherwise auto-discover a real lease
    mid-unit-test). The engine substrate solves the same problem the other way:
    a per-session JAR + PG with a per-test tenant is hermetic, so ambient
    leases cannot leak in either.

    Tests that exercise the resolver itself (``test_storage_mode.py``) carry
    their own ``_clean_storage_env`` autouse fixture that ``delenv``s the
    backend vars AFTER this one, so they still observe the true default. Any
    test that wants a specific backend sets ``NX_STORAGE_BACKEND[_<store>]``
    itself, which overrides this pin (later ``setenv`` wins).
    """
    # NX_TEST_T2_SUBSTRATE=none — provision NOTHING (nexus-lom9g / i711w).
    # For tests whose subject needs no T2 store at all: endpoint resolution,
    # env scrubbing, lease recovery. They previously said "=sqlite" to mean
    # "don't boot an engine", which worked only while a SQLite substrate
    # existed to fall back to. i711w deletes it, so the intent needs its own
    # spelling rather than riding on a backend that is about to vanish.
    # Checked FIRST: it is a statement about needing no substrate, not a
    # choice between two.
    selected = os.environ.get("NX_TEST_T2_SUBSTRATE")
    if selected == "none":
        return
    if selected == "sqlite":
        raise RuntimeError(
            "NX_TEST_T2_SUBSTRATE=sqlite: the SQLite test substrate was "
            "deleted with the SQLite T2 stores (nexus-i711w). The engine is "
            "the only substrate. If you meant 'this test needs no T2 store', "
            "that intent now has its own spelling: NX_TEST_T2_SUBSTRATE=none."
        )
    request.getfixturevalue("t2_service_env")


@pytest.fixture(autouse=True)
def _reset_shared_service_catalog_client() -> None:
    """Drop the process-lifetime shared SERVICE catalog client between tests
    (nexus-aqbrk).

    ``nexus.catalog.factory`` memoises ONE ``HttpCatalogClient`` for the life
    of the process (nexus-5en9j — it was the largest reconstruction count in
    the nexus-53x7s shakeout, 394 constructions in one run). Correct in
    production, where the tenant never changes mid-process. Wrong for a
    pytest session, where the engine substrate mints a FRESH TENANT AND TOKEN
    per test: the memoised client keeps the FIRST test's token, so every
    later test's catalog reads and writes land in the first test's tenant.

    The visible symptom is not "wrong tenant" — it is accumulation. Rows pile
    up in tenant #1 across the whole module, and eventually a
    ``register_owner`` that is the first of its name IN ITS OWN TEST hits a
    row an earlier test already wrote, and the engine correctly refuses:
    ``HTTP 409: integrity constraint violation`` on ``/v1/catalog/owners/
    upsert`` (catalog_owners_unique_name_type). Order-dependent, passes in
    isolation, and the error names a constraint rather than the cause — the
    same profile as the import-seed-id defect, and the same trap.

    ``reset_shared_service_catalog_client_for_tests`` already existed for
    exactly this; nothing called it outside the one test that owns the
    caching behaviour itself. Reset on BOTH sides so a test that constructs
    the client cannot leak it forward, and a test that inherits one cannot
    start dirty.
    """
    from nexus.catalog import factory

    factory.reset_shared_service_catalog_client_for_tests()
    yield
    factory.reset_shared_service_catalog_client_for_tests()


@pytest.fixture(autouse=True)
def _reset_service_t2_db() -> None:
    """Drop the process-lifetime service ``T2Database`` singleton between tests
    (nexus-aqbrk).

    THE SAME DEFECT AS ``_reset_shared_service_catalog_client`` ABOVE, one
    tier over, and named as such in nexus-5en9j: ``mcp_infra`` memoises ONE
    service-backed ``T2Database`` in ``_service_t2_db`` and every service-mode
    ``t2_index_write`` runs against it (``_service_t2_write_locked``). Its
    ``Http*Store`` clients bake in the endpoint and BEARER TOKEN they saw at
    construction, and the engine substrate mints a fresh tenant + token per
    test — so a singleton built by the first test writes every later test's
    rows into the FIRST test's tenant.

    The symptom is a test reading an empty store it just wrote to: the write
    landed in tenant #1, the read-back runs in its own tenant. Order-dependent
    — passes solo, fails in file order — and harmless on the SQLite substrate,
    where ``t2_index_write`` never takes the service branch and this reset is
    a no-op.

    Already diagnosed once, per-file: ``tests/test_rdr_084_plan_grow.py``
    carries a local autouse fixture calling ``reset_singletons()`` for exactly
    this reason. That is the same shape the catalog client had before the
    fixture above — one file working around a session-wide hazard. This
    promotes the eviction to the whole suite.

    SCOPE IS DELIBERATELY NARROWER THAN ``reset_singletons()``. That helper
    also drops ``_t1_instance`` / ``_t3_instance`` / ``_collections_cache`` /
    the plan cache / the vector client; making all of that autouse would
    invalidate module-scoped T1/T3 injections that tests legitimately expect
    to survive across a file. Only the credential-bearing T2 handle is evicted
    here. Reset on BOTH sides, for the same reason as the catalog client: a
    test cannot leak one forward, and cannot start dirty.
    """
    import nexus.mcp_infra as mcp_infra

    def _evict() -> None:
        with mcp_infra._service_t2_lock:
            if mcp_infra._service_t2_db is not None:
                mcp_infra._service_t2_db.close()
            mcp_infra._service_t2_db = None

    _evict()
    yield
    _evict()


@pytest.fixture(autouse=True)
def _reset_lease_resolution_history() -> None:
    """Reset ``service_endpoint``'s process-wide "ever resolved a lease"
    signal before AND after every test (nexus-7dsgp, critic round 1
    CRITICAL fix).

    The flag is deliberately process-lifetime in production (see its
    docstring), but a unit-test SESSION is one process shared across
    thousands of tests — without this reset, any earlier test that
    successfully calls ``discover_lease()`` (there are many, e.g. every
    ``_publish_lease``-based test in test_service_endpoint_discovery.py)
    would leave the flag ``True`` for the rest of the run, silently
    making a LATER, unrelated test's construction-time resolution
    failure retry-with-wait (a REAL 12s stall with no fake clock
    injected at most construction-time call sites) instead of the fast
    fail-loud that test actually expects — order-dependent pollution of
    exactly the kind nexus-1091 caught for the T3 side of this bead.
    """
    from nexus.db import service_endpoint

    service_endpoint.reset_lease_resolution_history_for_tests()
    yield
    service_endpoint.reset_lease_resolution_history_for_tests()


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect NEXUS_CONFIG_DIR so child processes write under tmp_path.

    nexus-mrmq: integration tests that dispatch ``claude -p`` subprocesses
    (the operator dispatch path, plan-runner, nx_answer equivalence
    suite) inherit the parent's ``os.environ``. Without this fixture
    the child resolves ``nexus_config_dir()`` to the user's real
    ``~/.config/nexus/`` and writes ``current_session`` /
    ``t1_addr.<claude_pid>`` files there. Reproduced 2026-05-08 during
    4.27.1 shakeout: a transient ``claude_dispatch -p`` subprocess
    rewrote the live MCP's session file and unlinked its addr file
    mid-session.

    Setting ``NEXUS_CONFIG_DIR`` here is read at call time inside
    ``nexus.config.nexus_config_dir()`` and propagates to children
    via ``os.environ`` inheritance, so every spawned subprocess
    (regardless of operator-dispatch mode) writes its config files
    under the per-test tmp dir.

    Tests that need to assert the default path (``Path.home() /
    .config / nexus``) explicitly ``monkeypatch.delenv`` first; that
    still works because this fixture's ``monkeypatch.setenv`` is
    overridden by any later test-local ``setenv`` / ``delenv`` call.

    Path layout mirrors the natural ``~/.config/nexus`` relative
    layout (``tmp_path/.config/nexus``) so per-test fixtures that
    set ``HOME=tmp_path`` and write into ``tmp_path/.config/nexus/``
    (e.g. ``test_scratch_cmd.fake_home``) land at the same path
    ``read_claude_session_id`` resolves to via ``NEXUS_CONFIG_DIR``.

    The directory itself is *not* pre-created — write helpers
    (``write_claude_session_id``, the T1 lease registry, etc.) all do
    ``parents=True, exist_ok=True`` themselves, and tests that
    explicitly call ``mkdir(parents=True)`` without ``exist_ok``
    on the same path would otherwise hit ``FileExistsError``.
    """
    config_dir = tmp_path / ".config" / "nexus"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture(autouse=True)
def _isolate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect NEXUS_CATALOG_PATH so tests never pollute the real user catalog.

    Without this, integration tests that trigger _catalog_hook() (via index_repo,
    index_markdown, or similar) register documents in the user's live catalog at
    ~/.config/nexus/. Before this fixture landed (RDR-060, 2026-04-08), 64
    orphan ``int-cce-*`` curator owners accumulated from
    ``test_cce_query_retrieves_cce_indexed_markdown`` alone.

    The fixture works because catalog write paths guard on
    ``Catalog.is_initialized(cat_path)`` — the tmp path is never initialised,
    so hooks return early. See ``tests/test_catalog_isolation.py`` for the
    regression tests that lock this behaviour in (nexus-dqr3 / nexus-b34f).
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "test-catalog"))


@pytest.fixture(autouse=True)
def _reset_aspect_worker_singleton() -> None:
    """Reset the module-level aspect_worker singleton around every test.

    nexus-u0u8a: ``aspect_extraction_enqueue_hook`` lazy-spawns a singleton
    daemon-thread worker via ``ensure_worker_started()`` for any
    supported-collection (knowledge__/rdr__/docs__) document hook. Only
    ``test_aspect_worker.py`` / ``test_aspect_drain_protocol.py`` reset it,
    so any OTHER test that fires such a hook leaks the singleton. The leaked
    worker keeps polling ``t2_index_write`` (degraded fallback to
    ``T2Database(default_db_path())``), and when a later test patches
    ``default_db_path`` to its own tmp db the worker claims + ``mark_done``s
    rows out from under that test — the exact mechanism behind the
    ``test_collection_rename`` aspect-cascade canary (debugger verdict
    2026-05-28: 95% repro). Resetting before AND after each test confines a
    spawned worker to its own test so it can never poll a sibling's db.
    """
    from nexus.aspect_worker import reset_worker_for_tests
    reset_worker_for_tests()
    yield
    reset_worker_for_tests()


# NO _reap_spawned_daemons fixture: it SIGTERMed any T2/T3 daemon a test had
# spawned into its own tmp NEXUS_CONFIG_DIR (nexus-scoo5 — real `nx upgrade`
# runs reached `nx daemon t2 ensure-running`, which spawned a DETACHED
# `nx daemon t2 start` that outlived the test body). It reaped tiers ("t2",
# "t3") only; T3's daemon retired in RDR-155 P4b and T2's in nexus-i711w
# Stage 2 sub-stage B, and both spawn paths are gone with them, so there is
# nothing left for it to find. Its implementation (tests/_daemon_leak_guard.py)
# and contract tests went with it.


def set_credentials(monkeypatch) -> None:
    """Set NX_LOCAL=0 (cloud/non-local posture) for tests that need it.

    Shared helper used by test_doc_indexer.py and test_pdf_subsystem.py.
    RDR-155 P4b: the CHROMA_* keys died with the chroma credential map and
    key presence no longer implies cloud mode — pin NX_LOCAL=0 explicitly.
    nexus-sghyo (2026-08-06): the VOYAGE_API_KEY env var this used to set
    alongside NX_LOCAL is no longer a client credential (client-side
    Voyage embedding retired, Hal determination 2026-07-28) and no
    surviving client code path (``_has_credentials()`` is deleted)
    reads it — kept here only in case a caller's mocked
    ``get_credential`` still keys off it incidentally.
    """
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk_test")


# RDR-109 Phase 1: cloud-mode opt-in fixture.
#
# Default test mode is local (no API keys, ONNX MiniLM EF). Tests that
# assert cloud-mode behavior — voyage-context-3 / voyage-code-3 embedder
# names, _has_credentials() gated paths, CloudClient routing — opt in via
# this fixture (or class-level
# ``pytestmark = pytest.mark.usefixtures("cloud_mode")``).
#
# The lint test ``test_mode_declarations_are_explicit`` enforces that any
# test function whose source contains ``voyage-(context|code)-3`` either
# depends on ``cloud_mode`` or is listed in ``_MODE_LINT_EXCLUDE`` below.
@pytest.fixture
def cloud_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate cloud mode: set Voyage/Chroma credentials and force
    ``nexus.config.is_local_mode`` to return False.

    Callers that do ``from nexus.config import is_local_mode`` inside a
    function body (the established pattern in this codebase — see all
    callsites under ``src/nexus/``) pick up the patch on next call.
    """
    set_credentials(monkeypatch)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)


# Tests whose source matches the voyage-token regex but legitimately do
# NOT need cloud_mode. Two granularities:
#   * ``_MODE_LINT_EXCLUDE_FILES`` — every test in the file is exempt.
#     Use for files whose tests are uniformly schema / name-shape /
#     canonical-set tests where the voyage token is a label, not a
#     behavior assertion.
#   * ``_MODE_LINT_EXCLUDE_NODEIDS`` — individual ``file.py::test_x``
#     entries for mixed files.
#
# Exclusion reasons fall into:
#   - "canonical-set": tests of ``corpus.canonical_embedding_model``
#     or ``CollectionName`` schema constants; the token is the schema's
#     canonical embedder name, not a behavior assertion.
#   - "string-literal-as-name": the test builds a conformant collection
#     name string and asserts on the *name shape* (RDR-103
#     ``<content_type>__<owner>__<model>__v<n>``), not on the embedder
#     that actually ran. The name is canonical regardless of mode.
#   - "parametrize-label": the voyage token appears only in a
#     ``pytest.mark.parametrize`` data tuple or test id.
#   - "docstring-or-comment": the voyage token appears only in a
#     docstring or comment, not in executable code.
#   - "mode-self-test": the test asserts local-mode behavior itself
#     (``test_local_mode.py``); cloud_mode would invert what it tests.
#
# Files that primarily exercise cloud-mode behavior (real Voyage calls,
# CloudClient routing, ``_has_credentials()`` gated paths) do NOT appear
# here; they declare ``pytestmark = pytest.mark.usefixtures("cloud_mode")``
# at module scope instead. See ``docs/contributing.md`` and
# ``tests/AGENTS.md``.
_MODE_LINT_EXCLUDE_FILES: frozenset[str] = frozenset({
    # "test_voyage_ef.py" REMOVED (nexus-sghyo, 2026-08-06): the file it
    # excluded, tests/db/test_voyage_ef.py, was deleted along with the
    # module it tested (nexus.db.voyage_ef) — client-side Voyage
    # embedding is retired (Hal determination 2026-07-28: "we do no
    # embedding on the client"). See _MODE_LINT_EXCLUDE_FILES_CEILING's
    # matching decrement in test_mode_declarations_are_explicit.py.
    # Cloud-behavior files — Phase 1 ships the lint mechanism with these
    # excluded; subsequent PRs promote each to module-level
    # ``pytestmark = pytest.mark.usefixtures("cloud_mode")``. Promotion is
    # per-file so each can be validated against the suite independently.
    # The lint test itself contains the regex.
    "test_mode_declarations_are_explicit.py",
    # RDR-109 Phase 2 dispatch tests intentionally name voyage tokens
    # to exercise the (mode, name) matrix. Voyage names here are the
    # subject under test, not assertions of cloud-mode behavior.
    "test_rdr_109_phase2_dispatch.py",
    # Local-daemon client-side embedding tests: voyage tokens are
    # collection-NAME fixtures / Fix-4 display-dispatch subjects; every
    # test pins mode explicitly via T3Database(local_mode=...), not the
    # ambient cloud_mode fixture.
    "test_local_daemon_client_embed.py",
    # RDR-169 G5 bridge address-field tests: voyage tokens appear only as
    # collection-NAME fixtures (knowledge__test__voyage-context-3__v1) in fully
    # mocked HttpVectorClient / _ServiceCollectionStub unit tests. The server
    # embeds, not these tests — they assert additive /v1 response shape and the
    # include_source_uri opt-in, never cloud-mode embedding behavior.
    "test_bridge_address_fields.py",
    # nexus-8o9pm voyage-capability gate: voyage tokens are collection-NAME
    # fixtures (footprint detection) and embedding_models inside a FAKE /version
    # response body; the gate is a pure data/HTTP predicate that never embeds, so
    # there is no ambient cloud_mode behavior to assert.
    "test_guided_upgrade_voyage_capability.py",
    # nexus-3l6gz multi-model combined-query grouping: voyage tokens are
    # collection-NAME fixtures driving _group_collections_by_model against a
    # fully-fake model-aware T3 stub — no embedder ever runs (the fake resolves
    # "embedding" by parsing the collection name), so the tests are
    # deployment-mode-agnostic; nothing asserts cloud-mode behavior.
    "test_combined_query_multimodel_bug.py",
    # RDR-001 managed-endpoint probe: voyage tokens appear only as
    # embedding_models inside a FAKE /version response body (injected
    # http_get) — the managed service's reported models, not cloud-mode
    # behavior. The test touches no credentials and pins nothing on
    # is_local_mode; the probe targets the unauthenticated /version handshake.
    "test_managed_endpoint.py",
    # nexus-vgq89 burn-down (2026-07-15): the Phase 1 "ships excluded,
    # subsequent PRs promote each" batch above (test_mode_declarations_are_
    # explicit.py .. test_managed_endpoint.py) predates this comment and is
    # left as-is. The 19 files below were that batch's un-promoted remainder
    # (conftest.py:530-548 in the pre-burn-down revision) — the promotion
    # was promised but never done. Each is now resolved one of two ways:
    # actually promoted (cloud_mode fixture added to the genuinely
    # cloud-behavior tests; file removed from this set — see
    # test_collection_cmd.py / test_doc_indexer.py / test_indexer_e2e.py /
    # test_integration.py / test_pdf_e2e.py / test_voyage_retry.py, now
    # living only as individual ``_MODE_LINT_EXCLUDE_NODEIDS`` entries
    # below plus their cloud_mode promotions), or kept here with an
    # honest per-file rationale (the remainder, below) matching the
    # documented-rationale pattern already used throughout the rest of
    # this set.
    #
    # Substantive-critic correction (2026-07-15, same-day follow-up): the
    # first pass of this burn-down wrote rationale for 5 files
    # (test_index_cmd.py, test_index_pdf_batch.py, test_index_rdr_cmd.py,
    # test_indexer.py, test_mcp_server.py) WITHOUT checking each file's
    # HEADER first — all 5 already carry a pre-existing module-level
    # ``pytestmark = pytest.mark.usefixtures("cloud_mode")`` (RDR-109
    # Phase 2), which means every test in them already satisfies the
    # lint's fixturenames check regardless of this exclusion set; the
    # "no is_local_mode() branch under test" rationale written for them
    # was simply false. All 5 are removed here as free wins. The
    # correction sweep also caught 9 MORE files with the same pre-existing
    # module mark sitting unnecessarily in the older "Schema /
    # canonical-set" block further below (test_catalog_cli.py,
    # test_catalog_collection_for.py, test_catalog_consolidation.py,
    # test_collection_name_migration.py, test_commands_dt.py,
    # test_corpus.py, test_indexer_conformant_names.py, test_rdr_hook.py,
    # test_registry.py) — removed there for the same reason. The sweep
    # also surfaced one rationale that was substantively wrong despite no
    # module mark: test_indexer_e2e.py's ``_pin_fake_voyage_key`` autouse
    # fixture makes its embedding-model assertions genuinely
    # credential-routing-dependent, not literal test data — promoted to
    # ``cloud_mode`` alongside the other 4 promotions above instead of
    # staying here.
    #
    # "chunker-param" class: the voyage token is passed as an explicit
    # ``target_model`` / collection-name string argument to a pure chunking
    # or CLI-normalization function (``_pdf_chunks``, ``_markdown_chunks``,
    # ``_collections_from_registry_info``-style name synthesis); no embedder
    # ever runs and no ``is_local_mode()`` branch is exercised by the
    # assertions. Equivalent to the "string-literal-as-name" class used
    # elsewhere in this set. No autouse fixture in this file's header sets
    # Voyage/Chroma credentials. Caveat:
    # ``test_staleness_check_uses_content_hash_when_catalog_absent`` DOES
    # call ``set_credentials(monkeypatch)`` directly in its body — but its
    # assertions (``where == {"content_hash": expected_hash}``, ``result
    # == 0``) only check the staleness query's WHERE clause and short-
    # circuit outcome, never the stored/resolved ``embedding_model`` value
    # (the mocked existing metadata's ``"voyage-context-3"`` is unused
    # test-fixture noise, per the test's own docstring: staleness falls
    # back to content_hash "which uniquely identifies an unchanged file
    # just as well as the legacy source_path key"). cloud_mode would be a
    # no-op declaration here too.
    "test_catalog_path.py",
    # "retry-mechanics" class: ``target_model`` is an opaque literal passed
    # into ``_index_code_file`` against a mocked collection/Voyage client;
    # the test proves retry-on-connect-error behavior, which does not
    # depend on deployment mode. No module-level fixtures/marks in this
    # file's header.
    "test_vector_retry.py",  # renamed from test_chroma_retry.py at RDR-155 P4b P0d
    # Whole-file "nxexp export/import format" class: every flagged test
    # constructs or reads a ``.nxexp`` header/record by hand (or via
    # ``export_collection``/``import_collection`` against a local
    # ``ephemeral_db``); ``embedding_model`` is header/record metadata being
    # validated, compared, or round-tripped — never an actual embedder
    # invocation. No test in this file reads ``is_local_mode()``. The two
    # flagged tests that DO call ``monkeypatch.setenv`` for Chroma/Voyage
    # credentials (``TestImportFlagsCLI`` and one other) do so only to
    # route the CLI's ``_t3()`` handle through the mocked/ephemeral db
    # argument, not to select an embedder — no header-level autouse
    # fixture is involved.
    "test_exporter.py",
    # "chunker-param" class (same as test_catalog_path.py): both flagged
    # tests call ``_pdf_chunks(..., target_model="voyage-context-3", ...)``
    # directly with a mocked ``PDFExtractor``/``PDFChunker`` — the model is
    # an opaque label passed through to chunk metadata, not something an
    # embedder produced. No module-level fixtures/marks in this file's
    # header.
    "test_pdf_chunks_no_silent_zero.py",
    # Same class, same header-verified absence of credential fixtures.
    "test_pdf_extractor.py",
    # Same class. This file's one autouse fixture (``_legacy_vector_backend``)
    # only pins ``NX_STORAGE_BACKEND_VECTORS=local`` (a vector-STORAGE-backend
    # axis, Chroma-direct vs service) — orthogonal to embedder mode. The
    # module docstring states outright: "prove that the pipeline stitches
    # together correctly without requiring API keys or network access."
    "test_pdf_subsystem.py",
    # nexus-vgq89 correction (2026-07-15, code-review-expert delta):
    # test_pdf_e2e.py's 4 flagged tests are NOT here. First-draft
    # rationale claimed "cloud_mode would be actively misleading" on the
    # theory that the module has no credentials and embeds purely
    # locally — WRONG. Every flagged test does
    # ``patch("nexus.config.get_credential", side_effect=lambda k:
    # "test-key")``, which makes ``is_local_mode()`` (it calls
    # ``get_credential("chroma_api_key")`` / ``get_credential(
    # "voyage_api_key")``) resolve to CLOUD unconditionally — so
    # ``effective_embedding_model_for_writes`` genuinely takes the cloud
    # branch and synthesizes the ``voyage-context-3`` collection-name
    # segment for real, not as a hardcoded label. The ACTUAL embedding
    # is separately forced local via a distinct
    # ``_embed_with_fallback`` override — two independent axes, and the
    # naming axis is the one this lint cares about. Promoted to
    # ``cloud_mode`` (replacing the fragile incidental get_credential
    # side-effect with the explicit, robust fixture — no behavior
    # change, since ``cloud_mode`` patches ``is_local_mode`` directly
    # and the embed override is untouched).
    #
    # "chunker-param / mocked-embed" class: all three flagged tests pass
    # ``target_model="voyage-context-3"`` directly into ``chunker_loop`` /
    # ``pipeline_index_pdf`` with ``_embed_with_fallback`` fully mocked
    # (return value hardcoded); no real embedder call, no
    # ``is_local_mode()`` branch under test. ``test_embed_fn_none_
    # resolves_credentials`` patches ``nexus.config.get_credential``
    # directly (not the ambient env) and only asserts the fallback got
    # CALLED, never which model it resolved to — the credential-resolution
    # WIRING is under test, not cloud-mode embedding behavior.
    "test_pipeline_stages.py",
    # Whole-file "mocked-store / collection-name" class: every flagged
    # test drives a mocked ``mock_store`` or a faked-transport
    # ``real_http_vector_client`` and asserts on the RDR-103-normalized
    # collection name a CLI flag was translated to — never a real embedder
    # call. Two flagged tests depend (via ``mock_store``) on the file's
    # ``env_creds`` fixture, which sets Chroma/Voyage credentials so
    # ``mock_store`` specs as ``HttpVectorClient`` rather than a local
    # ``T3Database`` — but the assertion under test is
    # ``t3_collection_name``'s auto-promotion, a pure function of the
    # collection-name PREFIX (``voyage_model_for_collection`` in
    # src/nexus/corpus.py never calls ``is_local_mode()``), so the
    # env_creds-driven handle TYPE is irrelevant to what's asserted.
    "test_store_cmd.py",
    # Schema / canonical-set / collection-name shape — mode-independent.
    #
    # nexus-vgq89 correction sweep (2026-07-15): test_catalog_cli.py,
    # test_catalog_collection_for.py, and test_catalog_consolidation.py
    # (previously listed between test_catalog_backfill_collections.py and
    # test_catalog_db.py) were removed here as free wins — each already
    # carries a pre-existing module-level ``pytestmark = pytest.mark.
    # usefixtures("cloud_mode")`` (RDR-109 Phase 2), making the blanket
    # file exclusion redundant. See the correction note above
    # test_catalog_path.py for the full sweep methodology.
    "test_backfill_hash.py",
    "test_catalog_backfill_collections.py",
    # Five entries removed (nexus-i711w terminal deletion): test_catalog_
    # collections_rebuild / concurrent_writer_lock / db / incremental_rebuild
    # / collections_owner_backfill died with the local catalog. DOWNWARD-only.
    "test_catalog_collection_name.py",
    "test_catalog_collections.py",
    # test_catalog_etl.py entry removed (nexus-i711w Stage 2 sub-stage A):
    # the file died with the SQLite->PG ETL readers. DOWNWARD-only edit.
    "test_catalog_doctor_collections_drift.py",
    # RDR-103 / nexus-j9ey + b03o advisor: voyage tokens appear in
    # synthetic collection names being asserted against, not as
    # cloud-mode behaviour under test.
    "test_catalog_doctor_name_vs_embed_dim.py",
    # test_upgrade_name_vs_embed_dim_advisory.py entry removed (RDR-158 P4
    # Stage 4, nexus-i711w): the file died with _run_upgrade's local leg.
    # DOWNWARD-only edit.
    # test_catalog_manifest_backfill.py entry removed (nexus-i711w terminal
    # deletion): the file's raw-Catalog harness died with the local catalog.
    # test_catalog_migrate_fallback.py entry removed (nexus-i711w terminal
    # deletion, DIE batch-b rm). DOWNWARD-only edit.
    "test_catalog_papers_curator_isolation.py",
    "test_catalog_rename_collection.py",
    "test_catalog_spans_chunk_char.py",
    "test_checkpoint.py",
    "test_collection_gc.py",
    # nexus-vgq89 correction sweep: test_collection_name_migration.py
    # removed here (same free-win reason as above — pre-existing module
    # cloud_mode mark).
    # RDR-137 P2a (nexus-tts0d.4): same voyage-token-in-fixture pattern
    # — the catalog-backed reader tests register synthetic conformant
    # collection names and read them back; no Voyage call.
    "test_repos_reader.py",
    # RDR-137 P4.3 (nexus-tts0d.17): same pattern — knowledge__ /
    # docs__ collection names used as fixtures for the catalog
    # writer+reader cycle; no Voyage call.
    "test_index_corpus_knowledge_e2e.py",
    # RDR-137 followup CRITICAL-3/4/5 (nexus-43qgm.3-5): voyage tokens
    # appear in synthetic conformant collection names used as
    # adapter-test fixtures; no Voyage call is ever made.
    "test_rdr137_followup_critical_345.py",
    # RDR-137 followup SIG-6/8/11 (nexus-43qgm.6,8,11): same pattern
    # — voyage tokens in synthetic collection-name fixtures for the
    # OQ-5 deterministic-ordering and catalog-missing observability
    # tests; no Voyage call.
    "test_rdr137_followup_reader_sigs.py",
    # RDR-137 followup SIG-10/13/14/17 (nexus-43qgm.10,13,14,17):
    # voyage tokens in adapter / context / collection synthetic
    # fixtures; no Voyage call.
    "test_rdr137_followup_batch_sigs.py",
    # RDR-137 followup IMP-18..27 (nexus-43qgm.18-27): voyage tokens
    # in list_sibling_collections + adapter fixtures; no Voyage call.
    "test_rdr137_followup_p2_batch.py",
    # RDR-137 P3.5 (nexus-tts0d.10): same pattern — phantom
    # docs__1-2188 in the regression fixture for nexus-9iw41.
    "test_context_catalog_cutover.py",
    # nexus-vgq89 correction sweep: test_commands_dt.py, test_corpus.py,
    # and test_indexer_conformant_names.py removed here (same free-win
    # reason — pre-existing module cloud_mode mark).
    "test_doc_indexer_hash_sync.py",
    "test_doctor_cmd.py",
    "test_doctor_integrity.py",
    "test_doctor_search.py",
    "test_indexer_duplicate_content.py",
    "test_indexer_modules.py",
    "test_indexer_utils_repo.py",
    "test_memory.py",
    "test_metadata_consistency.py",
    "test_metadata_extraction_source.py",  # RDR-139 Layer D: pure schema unit
    "test_metadata_schema.py",
    # RDR-139 Phase 2/3 (Layers C/D/E): pure metadata-schema / CLI-routing /
    # T2-store unit tests; the voyage-context-3 literal is an incidental
    # placeholder embedding_model / collection-name segment, not cloud-mode
    # behavior.
    "test_dt_content_layer_d.py",
    "test_dt_mcp_fallback.py",
    # test_document_highlights.py entry removed (nexus-i711w Stage 2
    # sub-stage A): the file died with the SQLite store. DOWNWARD-only edit.
    "test_dt_highlights_layer_e.py",
    "test_dt_capture_cmd.py",
    # test_migrations_rdr108_phase1c.py entry removed (RDR-158 P4 Stage 4,
    # nexus-i711w): the file died with db/migrations.py. DOWNWARD-only edit.
    "test_plan_run.py",
    # nexus-vgq89 correction sweep: test_rdr_hook.py (tests/hooks/) and
    # test_registry.py removed here (same free-win reason — pre-existing
    # module cloud_mode mark).
    "test_source_uri_home_key.py",
    "test_store_enrich_doc_id.py",
    "test_store_put_cli_parity.py",
    "test_t3_strict_collection_naming.py",
    "test_t3.py",
    "test_tuning_config.py",
    # Mode-self-tests — these assert local-mode behavior; cloud_mode
    # would invert what they test.
    "test_local_mode.py",
    # nexus-duoak.3 bench teardown-scope: voyage tokens appear only in
    # synthetic collection-name fixtures (REAL / after lists) exercising pure
    # set-difference logic in bench_tumblers/plan_teardown; no Voyage call is
    # ever made and no embedder mode is asserted.
    "test_teardown_scope.py",
    # nexus-h1zu0: dim_for_model_token(token) -> int|None is a pure dict
    # lookup (nexus.db.reconcile._MODEL_DIMS, mirroring the Java
    # PgVectorRepository.MODEL_DIMS authority). "voyage-code-3"/
    # "voyage-context-3" are passed as literal ARGUMENTS asserting the
    # routing TABLE's contents, not a behavior assertion about which
    # embedder ran — no Voyage call, no credential, no embedder mode
    # involved anywhere in the file. Reason class "string-literal-as-name".
    "test_h1zu0_dim_routing.py",
})

_MODE_LINT_EXCLUDE_NODEIDS: frozenset[str] = frozenset({
    # Reserved for individual mixed-file exclusions. Format:
    # "tests/test_file.py::test_func"  (no parametrize suffix).
    #
    # nexus-9n485 tombstone probe — reason: "string-literal-as-name". Both
    # tests pass "knowledge__1-1__voyage-context-3__v1" as the rename TARGET
    # of `nx catalog rename-collection`; the voyage token is one segment of a
    # conformant RDR-103 name, and what is asserted is the three-state
    # tombstone guard's refusal (exit != 0, "tombstoned"/"restore" in the
    # message). The HttpVectorClient's network boundary is patched in both,
    # so no embedder is constructed and no credential is read — cloud_mode
    # would add a live-credential dependency to a fully patched test without
    # changing a single assertion.
    "tests/test_catalog_rename_collection_tombstone_probe.py::test_rename_rejects_tombstoned_old_with_actionable_message",
    "tests/test_catalog_rename_collection_tombstone_probe.py::test_rename_rejects_tombstoned_new_as_not_free_to_claim",
    #
    # RDR-185 ladder — reason: "string-literal-as-name". Builds a conformant
    # RDR-103 collection NAME (or a CollectionClassification carrying the
    # name's model SEGMENT) and asserts on planning/rollback/re-id behaviour
    # keyed off that segment. It does not call a Voyage embedder: the rung
    # tests inject fakes for every collaborator, and the local bge-768 path
    # is what actually runs. cloud_mode would change nothing it asserts.
    #
    # This began as nine entries. Eight (test_rollback_via_map ×2,
    # test_substrate_leg ×4, test_substrate_rung ×2) were dropped in the
    # nexus-i711w liveness burn-down: 88d91bd5 deleted those files with the
    # Chroma migration machinery, and the entries had been dead ever since.
    # nexus-r5f3c — reason: "string-literal-as-config-value". The test's
    # subject is the SUPERVISOR's env-plumbing gate: a legacy config with
    # local.embed_model="voyage-context-3" must still plumb the credential
    # chain (the mirror of the bge-blocks-plumb case). Popen is mocked; no
    # embedder or cloud call exists. cloud_mode would change nothing.
    "tests/daemon/test_storage_service_daemon.py::TestSpawnServiceVoyageKeyPlumbing::test_voyage_configured_model_still_plumbs",
    "tests/upgrade/test_gap4_two_mechanisms.py::test_rung_convergence_is_re_derived_live_never_cached",
    #
    # REAL keyed integration tests (-m integration, @requires_voyage_key):
    # these derive cloud mode from GENUINE credentials — the cloud_mode
    # fixture would OVERWRITE the real VOYAGE_API_KEY with the "vk_test"
    # fake and break them against the live API (caught by the local-service
    # gate during the 6.10.1 release: voyageai AuthenticationError; the
    # default-marker full suite deselects -m integration, so only the gate
    # runs these). Their mode declaration is the requires-key gating itself.
    "tests/test_integration.py::test_voyage_code3_index_and_query",
    "tests/test_integration.py::test_cce_query_retrieves_cce_indexed_markdown",
    "tests/test_integration.py::test_t3_put_embedding_model_in_search_metadata",
    #
    # nexus-e0w01 / nexus-gednd (2026-07-13): "string-literal-as-name" class —
    # the voyage token appears only inside RDR-103-conformant collection-NAME
    # strings; the frecency test pins the service path via
    # NX_STORAGE_BACKEND_VECTORS + a mocked HttpVectorClient (no embedder
    # runs), and the tripwire tests mock get_t3/compute_assignments entirely.
    # RENAMED, not added (nexus-i711w Stage 2 sub-stage C): the first tripwire
    # entry below was `::test_local_path_failure_records_hook_failures_row`
    # until 9c0cff18 ported it to the service arm and renamed it. The reason
    # class is unchanged — `_force_service_path` mocks get_t3 and the captured
    # t2's compute_assignments, so still no embedder runs — but the old nodeid
    # no longer resolved, which silently converted a granted exclusion into a
    # non-exclusion and left this lint red on develop. Retargeting the pointer
    # keeps the count at 58; no ceiling bump is warranted for a rename.
    "tests/test_frecency_service_mode.py::TestFrecencyRdrCollection::test_rdr_collection_included_in_frecency_update",
    "tests/test_taxonomy_hook_tripwire.py::test_service_path_failure_records_hook_failures_row",
    "tests/test_taxonomy_hook_tripwire.py::test_tripwire_persist_failure_never_propagates",
    #
    # #1060: pure collection-NAME validation (length/charset) — references a
    # legacy voyage-named collection as realistic input but makes no cloud-mode
    # embedder assertion, so the cloud_mode fixture is not applicable.
    "tests/test_issue_1060_collection_name_overflow.py::test_short_known_voyage_name_passes",
    #
    # nexus-h8rf6.3: shape-conformance regression — a REAL HttpCatalogClient
    # (faked transport) flows through build_staleness_cache; the voyage token
    # appears only inside a conformant collection-name string used as data
    # ("string-literal-as-name" class). No embedder runs; no mode-dependent
    # path is exercised.
    "tests/catalog/test_docs_for_chashes_shape_conformance.py::TestBuildStalenessCacheConsumesRealHttpClient::test_no_raise_with_real_http_catalog_client",
    #
    # nexus-h8rf6 wave (expire/update_source_path/collection_metadata ports +
    # the 49523e16 live-content regression): all "string-literal-as-name" —
    # a REAL HttpVectorClient/HttpCatalogClient over a FAKED transport, with
    # the voyage token appearing only inside conformant collection-name
    # strings used as opaque data (or, for collection_metadata, asserting the
    # NAME-derived model parse). No embedder runs; no mode-dependent path.
    "tests/catalog/test_docs_for_chashes_live_content.py::TestBuildStalenessCacheLiveContent::test_nonzero_docs_after_index_like_write",
    "tests/test_http_vector_client_parity.py::TestExpire::test_expire_deletes_only_expired_knowledge_rows",
    "tests/test_http_vector_client_parity.py::TestExpire::test_expire_no_knowledge_collections_returns_zero",
    "tests/test_http_vector_client_parity.py::TestCollectionMetadata::test_returns_t3_parity_keys",
    #
    # nexus-gc2ze + nexus-c9xr2/u37lw wave (2026-07-04): all
    # "string-literal-as-name" — a REAL HttpCatalogClient/HttpVectorClient
    # over a FAKED transport; the voyage token appears only inside
    # conformant collection-name strings used as opaque identifiers (the
    # u37lw guard tests additionally assert the NAME-derived model parse,
    # same rationale as collection_metadata above). No embedder runs; no
    # mode-dependent path executes.
    "tests/catalog/test_http_catalog_client.py::TestResolveChunk::test_resolve_chunk_returns_full_dict",
    "tests/test_service_mode_cli_real_client.py::test_collection_reembed_dry_run_service_mode_real_client",
    "tests/test_service_mode_cli_real_client.py::test_collection_reembed_cross_model_rejected_service_mode",
    "tests/test_service_mode_cli_real_client.py::test_collection_reembed_same_model_requests_server_side_re_embed",
    #
    # RDR-152 nexus-gmiaf.22 (Seam B): asserts service-mode skips the embed
    # fallback. Voyage tokens appear only as realistic collection-NAME /
    # prepared-chunk-metadata fixtures (real docs collections ARE
    # voyage-context-3); the test never calls Voyage — service mode embeds
    # server-side — so it makes no cloud-mode embedder assertion and the
    # cloud_mode fixture is not applicable.
    "tests/test_indexer_seam_b_cutover.py::test_index_pdf_incremental_service_mode_skips_embed_fallback",
    #
    # RDR-152 nexus-qnp5s: catalog consumer migration tests. Voyage tokens
    # appear only as realistic collection-NAME fixtures in collections_by_owner
    # assertions (real collections ARE voyage-named); these test the catalog
    # public-API methods, not cloud-mode embedder behavior, so cloud_mode is
    # not applicable.
    # TestSQLiteCatalogNewMethods entry removed (nexus-i711w terminal
    # deletion): the SQLite parity arm retired. DOWNWARD-only edit.
    "tests/test_catalog_consumer_service_mode.py::TestHttpCatalogClientNewMethods::test_collections_by_owner",
    #
    # RDR-152 nexus-enehl: frecency metadata-update service client test. The
    # voyage token is a realistic collection-NAME fixture for the update-chunks
    # HTTP request body; the test asserts the request is POSTed to the
    # /update-metadata endpoint, not any cloud-mode embedder behavior.
    "tests/db/test_http_vector_client.py::TestUpdateChunks::test_posts_to_update_metadata_endpoint_empty_and_tenant",
    #
    # nexus-f0r8p.3 (RDR-181): force_re_embed forwarding tests in the batch-flush
    # closure. The voyage tokens are collection-NAME fixtures (code__repo__voyage-code-3__v1
    # etc.); the tests assert the force_re_embed kwarg is forwarded/omitted
    # correctly on the flush call, not any cloud-mode embedder behavior.
    "tests/test_indexer_seam_b_cutover.py::test_run_index_batch_flush_forwards_force_re_embed",
    "tests/test_indexer_seam_b_cutover.py::test_run_index_batch_flush_force_false_omits_force_re_embed",
    #
    # nexus-wxjr6 (2026-08-09, code review Important I1 follow-up):
    # test_run_index_batch_flush_retries_transient_failure_then_succeeds —
    # "string-literal-as-name", same class as the sibling pair immediately
    # above. The voyage token is the same collection-NAME fixture literal
    # passed to the mocked flush closure; the test asserts retry-after-
    # transient-failure control flow and single-side-effect sweep
    # accounting, not any cloud-mode embedder behavior.
    "tests/test_indexer_seam_b_cutover.py::test_run_index_batch_flush_retries_transient_failure_then_succeeds",
    #
    # nexus-3mwuo (2026-08-09, C1-residual from the wxjr6 delta re-review,
    # T2 review-wxjr6-client-2026-08-09 [22014]): shared-chash both-paths
    # fix test — same "string-literal-as-name" class as the sibling pair
    # above. The voyage token is the same collection-NAME fixture literal
    # passed to the mocked flush closure; the test asserts the shared
    # chash rides both the combined write and the legacy orphan upsert
    # call, not any cloud-mode embedder behavior.
    "tests/test_indexer_seam_b_cutover.py::test_run_index_batch_flush_shared_chash_orphan_copy_survives_identity_doc_failure",
    #
    # nexus-te885.8.1 (pg-source reconcile leg for verify-fill): builds a
    # mocked /v1/vectors/collections response using conformant collection-
    # NAME strings (code__nexus-1-1__voyage-code-3__v1,
    # knowledge__nexus-1-1__voyage-context-3__v1) purely as PgReadClient
    # list_collections() parsing test data. No embedder runs and no
    # mode-dependent path executes ("string-literal-as-name" class).
    "tests/migration/test_pg_read.py::TestListCollections::test_returns_name_objects",
    #
    # nexus-vgq89 burn-down (2026-07-15): test_collection_cmd.py promoted
    # out of the whole-file grandfathered exclusion above. Three of its
    # eight flagged tests are genuine cloud-embedder behavior (re-embed via
    # Voyage) and now carry the ``cloud_mode`` fixture directly; the
    # remaining five below are "string-literal-as-name" /
    # collection-name-DATA: ``_collections_from_registry_info`` and
    # ``run_collection_postprocessing`` tests build registry-info dicts
    # with conformant collection-name strings and fully mock
    # ``_discover_taxonomy``/``make_t3`` — no embedder runs. Note
    # ``test_collections_from_registry_info_filters_excluded`` and
    # ``..._prefers_conformant_code_collection`` do exercise
    # ``is_local_mode()`` indirectly (via ``taxonomy.local_exclude_
    # collections``), but neither test's actual assertions depend on which
    # branch fires — both only assert the always-unfiltered docs__/rdr__
    # names are present, never a code__ presence/absence — so cloud_mode
    # would be a no-op declaration, not a real promotion.
    "tests/test_collection_cmd.py::test_collections_from_registry_info_filters_excluded",
    "tests/test_collection_cmd.py::test_collections_from_registry_info_prefers_conformant_code_collection",
    "tests/test_collection_cmd.py::test_collections_from_registry_info_dedupes",
    "tests/test_collection_cmd.py::test_run_collection_postprocessing_does_not_pass_alias_through",
    # ``test_info_shows_embedding_model``: parametrized over
    # (collection_name, expected_model) pairs against a mocked ``mock_db``;
    # asserts the ``info`` command's display parses the model out of the
    # collection NAME, never a real embedder call.
    "tests/test_collection_cmd.py::test_info_shows_embedding_model",
    #
    # nexus-vgq89 burn-down (2026-07-15): test_doc_indexer.py promoted out
    # of the whole-file grandfathered exclusion above; 32 of its 36
    # flagged tests genuinely exercise cloud-embedder behavior (the
    # ``_embed_with_fallback``/CCE family, and the credential-gated
    # staleness/force/incremental-checkpoint family whose target_model
    # resolution depends on ``is_local_mode()``) and now carry the
    # ``cloud_mode`` fixture directly. The four below do not:
    # ``test_index_md_falls_back_to_local_embedder_when_no_credentials``
    # and ``test_make_local_embed_fn_returns_consistent_model_name`` are
    # mode-self-tests — they explicitly delete/never-set credentials to
    # prove the LOCAL fallback path; ``cloud_mode`` would invert what they
    # test (same "mode-self-test" class as test_local_mode.py above).
    "tests/test_doc_indexer.py::test_index_md_falls_back_to_local_embedder_when_no_credentials",
    "tests/test_doc_indexer.py::test_make_local_embed_fn_returns_consistent_model_name",
    # ``TestSectionTypeInPipeline``'s two tests call ``_markdown_chunks(md,
    # "abc123", "voyage-context-3", ...)`` directly — the model is an
    # opaque label argument to a pure chunking/section-classification
    # function; no embedder runs ("string-literal-as-name" / "chunker-param"
    # class, same as test_catalog_path.py above).
    "tests/test_doc_indexer.py::TestSectionTypeInPipeline::test_markdown_chunks_has_section_type",
    "tests/test_doc_indexer.py::TestSectionTypeInPipeline::test_markdown_chunks_section_classified",
    # nexus-8x4le (2026-08-05): landed with the RDR-180 chash-conformance
    # doctor check (dbd2cb46, nexus-du2dw) — reason "string-literal-as-name".
    # ``test_fully_routable_collections_still_render_plain_clean`` builds a
    # FAKE T3 client whose ``collection_names`` fixture is
    # ``["knowledge__x__bge-base-en-v15-768__v1", "code__y__voyage-code-3__v1"]``
    # purely to exercise the routability-enrichment probe's "all collections
    # route to a known dim" branch; the voyage token is a segment of a
    # conformant RDR-103 collection NAME fed to a fake, never to a real
    # embedder. No Voyage credential or client is constructed anywhere in
    # ``TestCheckChashConformanceReport``. Found via the whole-session
    # census once it was restored to the default loop (see the module
    # docstring above ``_MODE_LINT_EXCLUDE_FILES``/here for why this census
    # cannot live in the lint bucket) — the test itself is not new-broken,
    # it was simply never checked while the census sat mis-scoped.
    "tests/test_health_service_checks.py::TestCheckChashConformanceReport::test_fully_routable_collections_still_render_plain_clean",
    # nexus-sghyo PORT (2026-08-07): "string-literal-as-name". The test
    # manually seeds a fake misclassified chunk with a hardcoded
    # ``"embedding_model": "voyage-code-3"`` metadata literal to simulate
    # a pre-existing stale record — no embedder is constructed, and the
    # `cloud_mode` fixture it used to carry was dropped because it made
    # this test collide with the now-retired non-service-embedding leg
    # (indexer.py's CredentialsMissingError). The prune/migration
    # behaviour under test is embedding-model-independent.
    "tests/test_indexer_e2e.py::test_migration_moves_prose_from_code_to_docs",
    #
    # nexus-wxjr6 (2026-08-09, kl2z6 combined write client half):
    # "string-literal-as-name". Both tests assert the combined write's
    # request BODY carries the "collection" field verbatim
    # ("code__nexus-1-1__voyage-code-3__v1") against a monkeypatched
    # `_post` — no embedder runs, no mode-dependent path executes. The
    # voyage token is a conformant collection-name fixture the wire-shape
    # assertion happens to need, not a behavior assertion about which
    # embedder ran.
    "tests/catalog/test_manifest_write_many.py::TestWriteManifestManyCombined::test_combined_wire_shape",
    "tests/catalog/test_manifest_write_many.py::TestWriteManifestManyCombined::test_ack_echo_raises_when_chunks_written_absent",
    #
    # nexus-y9t08 / nexus-n2w4q (2026-08-10, the combined-write timeout and
    # retry follow-ups to wxjr6 immediately above): "string-literal-as-name",
    # the SAME reason class, same file, same call. All four tests exercise
    # `write_manifest_many(..., chunks=..., collection=...)` and need a
    # conformant RDR-103 collection name to reach the chunk-carrying branch
    # at all (that branch raises ValueError without a `collection` — see
    # `test_chunks_requires_collection`); they then assert on the POST's
    # kwargs (`timeout=600.0`, `retry_read_timeout=False`), the POST COUNT,
    # and the exception TYPE/classification. Nothing asserts on the
    # collection string itself, and no embedder can run: `_client()` is
    # `HttpCatalogClient.__new__` (no `__init__`, no config read, no
    # credential) and every one of the four monkeypatches `_post`, so the
    # name never leaves the request body. Requesting `cloud_mode` would add
    # a mode dependency to a fully-monkeypatched wire-behaviour unit test
    # without changing one assertion — the opposite of what that fixture is
    # for. Landed red on develop for the usual reason recorded throughout
    # this block: both authoring commits (127f2dbc, 47c5a39a) ran targeted
    # path-scoped batteries, and this census only fires on a whole-session
    # collection.
    "tests/catalog/test_manifest_write_many.py::TestWriteManifestManyCombinedTimeout::test_combined_write_gets_an_embed_grade_timeout",
    "tests/catalog/test_manifest_write_many.py::TestCombinedWriteReadTimeoutNotRetried::test_read_timeout_raises_converted_type_after_exactly_one_post",
    "tests/catalog/test_manifest_write_many.py::TestCombinedWriteReadTimeoutNotRetried::test_converted_exception_is_not_classified_as_connectivity",
    "tests/catalog/test_manifest_write_many.py::TestCombinedWriteReadTimeoutNotRetried::test_connection_reset_still_propagates_unconverted_for_retry",
})


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    """Provide a T2Database backed by the engine test substrate (all-HTTP).

    ``path`` is RETAINED-AND-IGNORED for signature stability (RDR-158 P4,
    SQLite retirement) — see ``T2Database.__init__``'s own docstring. Every
    domain store is an HTTP client over the engine's PG tables; there is no
    local ``.db`` file backing this fixture despite the path-shaped
    argument. The engine substrate self-provisions (or skips) via
    ``ensure_engine``/``mint_test_tenant`` elsewhere in this module.
    """
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


#: Process-wide unique id source for fidelity-import seeding (RDR-155
#: P4b P0a'). The topics PK is GLOBAL across tenants on the shared session
#: engine, so per-module counters collide across modules in one pytest
#: session (bisected finding). Every module that seeds preserved ids MUST
#: draw from THIS counter.
#:
#: THE STRIDE IS LOAD-BEARING (nexus-aqbrk, 2026-07-25). The note here used
#: to claim imports "preserve ids VERBATIM without advancing the engine's
#: serial sequences". That is false: TaxonomyRepository.importTopic ends with
#: advanceTopicsIdSequence(ctx, srcId), a setval to GREATEST(last_value,
#: srcId) — deliberately, so a migrated tenant's next live topic cannot
#: collide with its own imported ids. The consequence for a shared session
#: engine is that ONE import at N drags the global serial sequence to N, and
#: every subsequent ORDINARY topic creation (persist_discovered,
#: persist_rebuild, ...) in ANY tenant then consumes N+1, N+2, ... — walking
#: straight into the ids this counter is about to hand out. The next import
#: to draw an already-consumed id hits ON CONFLICT (id) against a row owned
#: by a different tenant, which RLS rejects as a WITH CHECK violation and the
#: handler reports as "supplied id is not available in this tenant".
#:
#: Symptom when this breaks: a cascade of HTTP 409s that looks like an
#: engine defect and is order-dependent (test_taxonomy.py failed at test 20;
#: deselecting three topic-creating tests moved it to test 33). A larger
#: starting offset does NOT help — the serial path just follows the counter
#: up from wherever it lands. The STRIDE is what fixes it: after an import
#: at N, ordinary inserts would have to burn a million ids to reach N + STEP,
#: and a pytest session creates thousands.
import itertools

_IMPORT_SEED_ID_BASE = 1_000_000_000
_IMPORT_SEED_ID_STEP = 1_000_000

_import_seed_ids = itertools.count(_IMPORT_SEED_ID_BASE, _IMPORT_SEED_ID_STEP)


def next_import_seed_id() -> int:
    """Session-unique id for fidelity-import seeding (see note above)."""
    return next(_import_seed_ids)


def make_vector_test_client():
    """THE test vector substrate (RDR-155 P4b P0a): a fresh
    ``InMemoryVectorClient`` with the real MiniLM default EF.

    The single replacement idiom for inline ``chromadb.EphemeralClient()``
    test constructions — semantics pinned differentially against the
    chroma oracle by ``tests/test_vector_substrate_contract.py``. Real
    per-instance isolation (no SharedSystemClient shared-state gotcha).
    EF (P0b decision, settled): the nexus-owned MiniLMDirect — real
    semantics (ranking snapshots and cosine gates are load-bearing),
    byte-parity with chroma's retired default EF pinned by
    tests/db/test_minilm_direct.py, zero chromadb involvement.
    """
    from nexus.db.inmemory_vector_store import InMemoryVectorClient
    from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction

    return InMemoryVectorClient(
        default_embedding_function=MiniLMDirectEmbeddingFunction()
    )


@pytest.fixture
def local_t3() -> T3Database:
    """T3Database backed by a fresh InMemoryVectorClient and DefaultEmbeddingFunction.

    Each test gets a fresh, isolated database — no API keys required.
    DefaultEmbeddingFunction uses the bundled ONNX MiniLM-L6-v2 model,
    so semantic similarity works correctly without Voyage AI.
    """
    from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction

    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=MiniLMDirectEmbeddingFunction(),
    )


# ── PDF fixture generators ─────────────────────────────────────────────────

_PAGE_TOPICS = [
    "Apple orchards produce fruit in autumn harvests.",
    "Database transactions ensure ACID consistency in storage systems.",
    "Network protocols define communication rules between distributed nodes.",
]


def _make_simple_pdf(path: Path) -> None:
    """1-page TrueType PDF with embedded metadata."""
    import pymupdf  # lazy

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 100),
        "Hello World. This is a test document for PDF ingest.",
        fontsize=12,
    )
    doc.set_metadata({
        "title": "Test Document",
        "author": "Test Author",
        "subject": "PDF Ingest Testing",
        "keywords": "test, pdf, nexus",
        "creationDate": "D:20260301000000",
    })
    doc.save(str(path))
    doc.close()


def _make_multipage_pdf(path: Path) -> None:
    """3-page TrueType PDF with semantically distinct content per page.

    Each page uses insert_textbox to fill a text rectangle (~2000 chars).
    This ensures:
    - PDFChunker(chunk_chars=100) produces multiple chunks (AC-U9/U10).
    - PDFChunker with the default 1500-char limit produces at least one
      dedicated chunk per page for reliable page attribution in E2E tests (AC-E2).
    """
    import pymupdf  # lazy

    doc = pymupdf.open()
    rect = pymupdf.Rect(72, 72, 523, 750)
    for topic in _PAGE_TOPICS:
        page = doc.new_page()
        text = f"{topic} " * 30
        page.insert_textbox(rect, text.strip(), fontsize=12)
    doc.set_metadata({"title": "Multipage Test", "author": "Test Author"})
    doc.save(str(path))
    doc.close()


def _make_type3_pdf(path: Path) -> None:
    """Generate a minimal valid PDF with a Type3 font as raw bytes.

    A ~600-byte hand-crafted PDF:
    - Object 3 (page) resources reference font object 5 as /F1
    - Object 5 is a Type3 font with a single glyph 'A' defined via CharProcs
    - Object 6 is the CharProcs stream for 'A' (d0 + filled box)
    - Object 4 is the page content stream (draws 'A' using /F1)

    Docling handles Type3 fonts via its own text extraction layer.
    get_text() on a Type3 glyph returns '' or 'A' depending on pymupdf
    version — used by pymupdf_normalized fallback if Docling fails.
    """
    glyph_stream = b"100 0 d0\n0 0 100 100 re f\n"
    content_stream = b"BT /F1 12 Tf 100 700 Td (A) Tj ET\n"

    obj_bodies = [
        # 1: catalog
        b"<</Type/Catalog/Pages 2 0 R>>",
        # 2: pages tree
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        # 3: page — resources point at font object 5
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        # 4: content stream
        b"<</Length " + str(len(content_stream)).encode() + b">>"
        b"\nstream\n" + content_stream + b"endstream",
        # 5: Type3 font dictionary; CharProcs references object 6
        b"<</Type/Font/Subtype/Type3"
        b"/FontBBox[0 0 100 100]"
        b"/FontMatrix[0.01 0 0 0.01 0 0]"
        b"/FirstChar 65/LastChar 65/Widths[100]"
        b"/CharProcs<</A 6 0 R>>"
        b"/Encoding<</Type/Encoding/Differences[65/A]>>>>",
        # 6: glyph procedure stream for 'A'
        b"<</Length " + str(len(glyph_stream)).encode() + b">>"
        b"\nstream\n" + glyph_stream + b"endstream",
    ]

    header = b"%PDF-1.4\n"
    body_parts: list[bytes] = []
    offsets: list[int] = []
    pos = len(header)
    for i, body in enumerate(obj_bodies, start=1):
        obj_bytes = f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        offsets.append(pos)
        pos += len(obj_bytes)
        body_parts.append(obj_bytes)

    body = b"".join(body_parts)
    xref_pos = len(header) + len(body)
    n = len(obj_bodies) + 1  # includes free entry 0
    xref = b"xref\n" + f"0 {n}\n".encode()
    xref += b"0000000000 65535 f\r\n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n\r\n".encode()
    trailer = (
        b"trailer\n<</Size " + str(n).encode() + b"/Root 1 0 R>>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    path.write_bytes(header + body + xref + trailer)


@pytest.fixture(scope="session")
def pdf_fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate all PDF test fixtures once per test session."""
    d = tmp_path_factory.mktemp("pdf_fixtures")
    _make_simple_pdf(d / "simple.pdf")
    _make_multipage_pdf(d / "multipage.pdf")
    _make_type3_pdf(d / "type3_font.pdf")
    return d


@pytest.fixture(scope="session")
def simple_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "simple.pdf"


@pytest.fixture(scope="session")
def multipage_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "multipage.pdf"


@pytest.fixture(scope="session")
def type3_pdf(pdf_fixtures_dir: Path) -> Path:
    return pdf_fixtures_dir / "type3_font.pdf"


# ── RDR-157 P3.4: synthetic PG bundle factory (bead nexus-vwvv5.13) ─────────────


@pytest.fixture
def make_pg_bundle_txz():
    """Factory building a synthetic ``nexus-pg-*.txz`` for bundle-extract tests.

    Mirrors the real P3.1 artifact shape: a ``bundle/`` root containing
    ``bin/{initdb,pg_ctl,psql,createdb}`` (stub executables), ``include/``,
    ``lib/``, ``share/``, and the ``.build_prefix`` relocation marker that
    ``scripts/build_pg_bundle.sh`` stamps. Single source of truth so a layout
    change (e.g. a new required binary) is a one-site edit.
    """
    import tarfile

    def _factory(tmp: Path, name: str = "nexus-pg-test.txz", *, with_build_prefix: bool = True) -> Path:
        staging = tmp / f"_stage_{name}"
        bundle = staging / "bundle"
        bin_dir = bundle / "bin"
        bin_dir.mkdir(parents=True)
        for b in ("initdb", "pg_ctl", "psql", "createdb"):
            f = bin_dir / b
            f.write_text("#!/bin/sh\nexit 0\n")
            f.chmod(0o755)
        for sub in ("include", "lib", "share"):
            (bundle / sub).mkdir()
        if with_build_prefix:
            (bundle / ".build_prefix").write_text("/build/prefix/nexus-pg\n")
        archive = tmp / name
        with tarfile.open(archive, "w:xz") as tf:
            tf.add(bundle, arcname="bundle")
        return archive

    return _factory


# ── docling model availability (nexus-c7gnx) ─────────────────────────────────
#
# The docling PDF extractor loads its layout + TableFormer models from the
# HuggingFace cache; when they are absent (offline, cold cache) docling raises
# LocalEntryNotFoundError and the extractor SILENTLY falls back to PyMuPDF
# (extraction_method='pymupdf_normalized'). Tests that assert
# extraction_method=='docling' then fail with a confusing assertion rather than
# a clear "models unavailable" signal. CI pre-fetches the models and HARD-FAILS
# if it cannot (see .github/workflows/ci.yml), so in CI the models are always
# present and these guards never skip. The skip only fires on a local run with a
# cold HF cache — turning a baffling fallback-assertion failure into a clean skip.


@pytest.fixture(scope="session")
def docling_available(tmp_path_factory: pytest.TempPathFactory) -> bool:
    """True iff docling actually performs the extraction (models present).

    Faithful probe: docling loads models lazily at convert() time, so we run a
    real extraction on a tiny generated PDF and check the SAME signal the tests
    assert (extraction_method == 'docling'). A cold/offline model cache makes the
    extractor fall back to PyMuPDF, which this detects as unavailable.

    Known limitation: the probe CANNOT distinguish "models unavailable"
    (environmental, skipping is correct) from "docling regressed in CODE so the
    extractor fell back" (a real bug) — both surface as extraction_method !=
    'docling'. This is acceptable because CI does NOT rely on the skip: the
    pre-fetch step (scripts/ci_warm_docling.py) runs this same probe and
    HARD-FAILS the job, so a docling code regression goes CI-red at pre-fetch.
    The skip is a local-developer convenience only; see require_docling.
    """
    try:
        import pymupdf

        probe = tmp_path_factory.mktemp("docling-probe") / "probe.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "docling availability probe")
        doc.save(str(probe))
        doc.close()

        from nexus.pdf_extractor import PDFExtractor

        result = PDFExtractor().extract(probe)
        return result.metadata.get("extraction_method") == "docling"
    except Exception:
        return False


@pytest.fixture
def require_docling(docling_available: bool) -> None:
    """Skip the requesting test when docling did not perform the extraction.

    Composes with the CI pre-fetch hard-fail: in CI the models are guaranteed
    present so this never skips; locally it skips cleanly instead of failing on
    the silent PyMuPDF fallback.
    """
    if not docling_available:
        pytest.skip(
            "docling did not perform the extraction. Locally this almost always "
            "means a cold/offline HuggingFace model cache; it can ALSO indicate a "
            "docling regression. CI does not rely on this skip — its pre-fetch step "
            "runs the same probe and HARD-FAILS (red), which is what distinguishes a "
            "genuine regression from a missing local cache. This skip only fires on "
            "a local run."
        )


# --- nexus-1odsl: reap test daemons the suite leaked --------------------------
#
# TWO process classes leak, for two DIFFERENT reasons, and a fix for one does
# not touch the other:
#
#   aspect-worker  `stop_worker()` stops the IN-PROCESS singleton thread. It
#                  does not touch the DETACHED `nx daemon aspect-worker start`
#                  subprocess that `ensure_aspect_worker_daemon` spawns (Popen
#                  + start_new_session), so any test exercising the auto-spawn
#                  path leaves a real daemon behind. Nothing reaps a daemon.
#
#   postgres       tests/_engine_substrate.py DOES clean up, via
#                  atexit.register(_teardown). But atexit does not run on
#                  SIGKILL, on a double Ctrl-C, or on a hard crash -- i.e.
#                  exactly how a long suite actually gets aborted. So the
#                  cluster survives with its postmaster still listening.
#
# They are not inert. On 2026-07-24, six workers and three postmasters were
# found still running from finished runs, and leaked workers produced a
# 1,375-entry burst of 401s against the PRODUCTION cloud endpoint on
# 2026-07-10: test daemons polling prod with a stale token.
#
# WHY A SESSION-START PASS AND NOT ONLY SESSION-END. A session-END reaper can
# only see its OWN basetemp, and by construction cannot run at all when the
# run is killed -- which is the case that leaks. Everything stranded by an
# aborted run is therefore reachable only from a LATER session, so the start
# pass is what actually drains the backlog (21 stale session dirs were present
# on this box when the bead was fixed). The end pass is kept for the
# aspect-worker case, which leaks even on a clean exit.
#
# SCOPE IS THE WHOLE POINT. Only processes whose own path argument lies under
# a pytest tmp root are signalled. A broad "kill anything named postgres"
# would kill the developer's real database.

#: (label, argument flag naming the process's own directory, signal to send).
#: The flag is what makes the match precise: it is the process's OWN state
#: directory, so a match cannot be a coincidence of some unrelated process
#: merely mentioning a tmp path.
_LEAK_SPECS: tuple[tuple[str, str, str], ...] = (
    # SIGTERM: the worker's normal shutdown signal.
    ("aspect-worker", "--config-dir ", "TERM"),
    # SIGQUIT is postgres's IMMEDIATE shutdown. SIGTERM would be a "smart"
    # shutdown that WAITS for clients to disconnect, which can hang forever on
    # a stranded cluster. These clusters are throwaway (fsync=off), so there is
    # nothing to protect by shutting down gracefully.
    ("postgres", "-D ", "QUIT"),
)


def _ps_all() -> list[tuple[int, str]]:
    """(pid, cmdline) for every live process. Never raises."""
    import subprocess  # noqa: PLC0415 -- deferred; teardown-only path

    out = subprocess.run(
        ["ps", "-eo", "pid=,command="], capture_output=True, text=True, timeout=10,
    ).stdout
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        if not pid_s.isdigit():
            continue
        rows.append((int(pid_s), cmd))
    return rows


# Back-compat alias: the reaper's own tests import this name.
_ps_aspect_workers = _ps_all


def _matches_class(cmd: str, label: str) -> bool:
    """Is *cmd* an instance of the *label* process class?

    postgres is matched on the executable's BASENAME rather than a substring so
    that neither `pg_ctl -D ...` (transient, also carries -D) nor postgres's own
    `postgres: checkpointer` worker processes are swept up. Killing the
    postmaster reaps its children anyway.
    """
    if label == "postgres":
        head = cmd.split(maxsplit=1)[0] if cmd.split() else ""
        return Path(head).name == "postgres"
    return "aspect-worker" in cmd or "aspect_worker" in cmd


def reap_leaked_test_daemons(
    *,
    tmp_root: Path,
    labels: tuple[str, ...] | None = None,
    _list_procs=_ps_all,
    _kill=None,
) -> list[tuple[str, int]]:
    """SIGNAL test daemons whose own state directory is under *tmp_root*.

    Returns (label, pid) for each process actually signalled. Never raises:
    this runs at session setup/teardown, where an exception would turn a
    tidy-up into a suite error and mask the real result.

    *labels* restricts which classes are considered. It gates the KILL, not the
    return value -- filtering afterwards would signal a process and then omit it
    from the report, which is strictly worse than not filtering at all.
    """
    if _kill is None:
        import os as _os  # noqa: PLC0415 -- deferred; teardown-only path
        import signal as _signal  # noqa: PLC0415 -- deferred; teardown-only path

        def _kill(pid: int, sig: str) -> None:  # noqa: ANN202 -- local default
            _os.kill(pid, getattr(_signal, f"SIG{sig}"))

    try:
        procs = list(_list_procs())
    except Exception:  # noqa: BLE001 -- a teardown probe is never a verdict
        return []

    # RESOLVE both sides. On macOS the tmp root is handed to us as
    # /var/folders/... while the spawned process carries the realpath
    # /private/var/folders/... (/var is a symlink to /private/var). A plain
    # string prefix compare silently matches NOTHING -- which is exactly how
    # this reaper passed its unit tests and still reaped zero real daemons on
    # its first end-to-end run.
    try:
        root = Path(tmp_root).resolve()
    except Exception:  # noqa: BLE001 -- teardown probe
        root = Path(tmp_root)

    reaped: list[tuple[str, int]] = []
    for pid, cmd in procs:
        for label, marker, sig in _LEAK_SPECS:
            if labels is not None and label not in labels:
                continue
            if not _matches_class(cmd, label):
                continue
            idx = cmd.find(marker)
            if idx < 0:
                continue
            tail = cmd[idx + len(marker):].split()
            if not tail:
                continue
            # `ps -eo command=` returns argv joined by spaces, UNQUOTED, so a
            # directory containing a space is split across tokens. Taking
            # tail[0] truncates it, the path resolves somewhere else, the
            # containment test fails, and a real leaked daemon is left running
            # with NO error -- a silent false negative. Found by review; the
            # fake-ps unit tests all used space-free tmp paths.
            #
            # So try progressively longer token joins until one resolves under
            # the root, stopping at the next flag.
            own_dir = None
            for k in range(1, len(tail) + 1):
                if k > 1 and tail[k - 1].startswith("-"):
                    break                       # ran into the next flag
                candidate = " ".join(tail[:k])
                # Absolute only: a relative value would resolve against the
                # REAPER's cwd, not the target process's cwd at spawn time.
                # Both current call sites pass absolute paths; refusing
                # relative ones keeps a future call site from silently
                # matching the wrong directory.
                if not candidate.startswith("/"):
                    break
                try:
                    resolved = Path(candidate).resolve()
                except Exception:  # noqa: BLE001 -- unparseable is not ours
                    continue
                if resolved.is_relative_to(root):
                    own_dir = resolved
                    break
            if own_dir is None:
                continue
            try:
                _kill(pid, sig)
            except Exception:  # noqa: BLE001 -- already exited / not ours anymore
                continue
            reaped.append((label, pid))
            break
    return reaped


def reap_leaked_aspect_workers(
    *,
    tmp_root: Path,
    _list_procs=_ps_all,
    _kill=None,
) -> list[int]:
    """Aspect-worker-only view of :func:`reap_leaked_test_daemons`.

    Retained because it is the documented entry point and its ``_kill`` takes a
    single pid; the generalised reaper's takes (pid, signal).

    Passes ``labels`` through rather than filtering the RESULT: filtering after
    the fact would still have signalled every postgres it walked past while
    reporting none of them. The pre-existing
    ``test_spares_unrelated_processes_that_mention_the_tmp_root`` asserts on the
    kill list, not the return value, and caught exactly that.
    """
    def _shim(pid: int, _sig: str) -> None:
        if _kill is None:
            import os as _os  # noqa: PLC0415 -- deferred
            import signal as _signal  # noqa: PLC0415 -- deferred
            _os.kill(pid, _signal.SIGTERM)
        else:
            _kill(pid)

    return [
        pid
        for _label, pid in reap_leaked_test_daemons(
            tmp_root=tmp_root, labels=("aspect-worker",),
            _list_procs=_list_procs, _kill=_shim,
        )
    ]


def stale_pytest_roots(basetemp: Path) -> list[Path]:
    """Sibling pytest session dirs from OTHER runs, newest-first.

    pytest lays sessions out as ``<tmp>/pytest-of-<user>/pytest-<n>``. The
    current session's own dir is excluded -- reaping it would kill the run in
    progress.

    Assumes ONE pytest run at a time on a box, which is this project's standing
    rule (feedback_no_parallel_tests). A second concurrent run's daemons would
    be reaped by this. That is why every reap is PRINTED rather than silent.
    """
    try:
        parent = Path(basetemp).resolve().parent
        current = Path(basetemp).resolve()
    except Exception:  # noqa: BLE001 -- probe
        return []
    if not parent.is_dir() or not parent.name.startswith("pytest-of-"):
        return []
    try:
        sibs = [
            d for d in parent.iterdir()
            if d.is_dir() and d.name.startswith("pytest-") and d.resolve() != current
        ]
    except Exception:  # noqa: BLE001 -- probe
        return []
    return sorted(sibs, key=lambda d: d.name, reverse=True)


@pytest.fixture(scope="session", autouse=True)
def _reap_leaked_test_daemons(tmp_path_factory):
    """Drain the backlog at START; catch this run's own leaks at END.

    Deliberately session-scoped and autouse: more than one test reaches the
    spawn paths, and a per-test fixture would have to be remembered by each of
    them -- which is exactly what failed. This catches the class.
    """
    basetemp = Path(tmp_path_factory.getbasetemp())

    # START: everything stranded by earlier aborted runs. This is the only
    # place those are reachable -- the run that leaked them was killed before
    # any teardown of its own could run.
    # ONE `ps` for the whole scan. The naive loop calls it once per stale root,
    # and there were 21 stale roots on the dev box when this landed -- 21 process
    # listings before the first test runs. The snapshot going stale mid-scan is
    # harmless: killing an already-exited pid raises, and that is caught.
    stale_roots = stale_pytest_roots(basetemp)
    drained: list[tuple[str, int]] = []
    if stale_roots:
        try:
            snapshot = _ps_all()
        except Exception:  # noqa: BLE001 -- a startup probe is never a verdict
            snapshot = []
        for stale in stale_roots:
            drained.extend(
                reap_leaked_test_daemons(tmp_root=stale, _list_procs=lambda: snapshot)
            )
    if drained:
        print(f"\n[nexus-1odsl] reaped {len(drained)} daemon(s) stranded by "
              f"earlier runs: {drained}")

    yield

    # END: this run's own leaks, on a clean exit. Cannot fire on a kill; that
    # is what the START pass above is for.
    reaped = reap_leaked_test_daemons(tmp_root=basetemp)
    if reaped:
        print(f"\n[nexus-1odsl] reaped {len(reaped)} leaked daemon(s): {reaped}")
        print(f"\n[nexus-1odsl] reaped {len(reaped)} leaked aspect-worker daemon(s): {reaped}")


def fake_credentials(value: str = "test-key", *, passthrough: tuple[str, ...] = (
    "service_url", "service_token",
)):
    """A ``get_credential`` side_effect that does NOT poison the endpoint.

    nexus-aqbrk. The common form in indexer tests is::

        patch("nexus.config.get_credential", side_effect=lambda k: "test-key")

    which answers EVERY key — including ``service_url``. That key is not a
    generic credential: it is the authoritative FULL service endpoint
    (``service_endpoint.py``: "used VERBATIM ... NX_SERVICE_URL env FIRST,
    then nx config set service_url"). So the blanket stub hands endpoint
    resolution the literal string "test-key", every client builds
    ``base_url="test-key"``, and the first request dies on
    ``httpx.UnsupportedProtocol: Request URL is missing an 'http://' or
    'https://' protocol``.

    Invisible on the SQLite arm, because nothing resolves a service endpoint
    there. Under the engine substrate it was the single largest failure
    cause found in this port — 29 of 32 in tests/test_indexer_e2e.py plus 4
    in tests/test_indexer_duplicate_content.py.

    This keeps the blanket answer for the credential the tests actually care
    about (embedder routing keys on ``voyage_api_key`` PRESENCE) while
    delegating the endpoint keys to the real resolver, so the substrate's
    own configuration survives the mock. It is the same orthogonality bug
    ``t2_service_env``'s NX_LOCAL pin documents: a stub chosen for one axis
    silently perturbing a neighbouring one.
    """
    from nexus.config import get_credential as _real

    def _side_effect(key: str):
        if key in passthrough:
            return _real(key)
        return value

    return _side_effect
