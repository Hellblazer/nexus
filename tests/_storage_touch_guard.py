# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage-touch guard for the opt-in test tenant (nexus-j23ni).

A test gets a minted tenant only when it asks for one (the ``t2_tenant`` /
``t2_service_env`` fixture or the ``t2_tenant`` marker). That is the default
substrate (``NX_TEST_T2_SUBSTRATE`` unset, or ``optin``); ``per-test`` is the
escape hatch that mints for every test, and ``none`` mints nothing. This module is
what keeps the default safe: a test that reaches storage WITHOUT a tenant must be
named, even when the code under test catches the error and carries on.

How it sees a touch: with no endpoint resolvable, every storage client raises a
guarded error (``GUARDED`` below: the resolver's not-resolvable type, the
managed-service unreachable / incompatible pair, the service_url-without-token
type, and T1's not-found type). A swallowed raise leaves no trace in the test's
outcome, so the guard patches ``__init__`` of each class at CLASS level (it
survives the import-time bindings in the clients) and records, per
construction, the test id, the phase (setup / call / teardown), the fixture
being set up, the origin frame (outermost frame under ``tests/`` or the item's
file) and a short stack. A construction on another thread is recorded against
the running test with source ``thread`` and is a finding like any other (its
own ``thread_touched`` bucket). ``subprocess.Popen.__init__`` is patched to count
the Python children a test starts (``children_spawned``). ``threading.Thread.__init__`` is patched to
record the test running when each thread was created: a thread a FINISHED test
leaked is listed in ``late_thread_touches`` under its owner, never charged to
the test that happens to be running (a thread with no recorded owner, created
outside ``threading.Thread``, is attributed to the running test).

Nothing here raises into the code under test: the recorders swallow their own
errors and count them (``guard_errors``; any is a vacuity problem).

Per test, at teardown:

* a construction while no tenant was active is a finding, unless the test
  carries ``expects_unresolvable_endpoint`` (resolver-subject tests, which
  raise it on purpose);
* an ``expects_unresolvable_endpoint`` test that passed without constructing
  any guarded error is a STALE exemption, also a finding.

Soft peeks: code in ``src/nexus`` also READS the endpoint without ever
constructing an error (``discover_lease``, ``get_credential`` for
``service_url`` / ``service_token``). Those calls are recorded, while no tenant
is active, against the first ``src/nexus`` frame outside ``service_endpoint.py``,
``http_vector_client.py`` and ``config.py`` (the consumer), with the ``tests/``
origin. A test that peeked and touched nothing else is ``peek_only``, its own
bucket and not clean; the list per site names those tests. Peeks never fail a
test.

Child processes: with the guard active it puts ``tests/_storage_touch_shim`` on
``PYTHONPATH`` (its ``sitecustomize`` installs the same class patches in every
child Python process and appends each construction to
``NX_STORAGE_TOUCH_CHILD_LOG``, attributed through ``PYTEST_CURRENT_TEST``).
The parent folds the lines into the test that spawned the child: touches
(deduped and capped like its own), soft peeks, and one ``child_start`` per child
(``children_seen``). A line whose test has already finished is a late child
touch: reported uncapped, printed in the terminal summary, and the owning module
is marked.

``NX_STORAGE_TOUCH_GUARD_MODE`` is ``fail`` (default: the teardown raises, naming
the test) or ``report`` (findings go to a report, the test is untouched). Either
way the session verdict (``vacuity`` in the report, printed in the summary) lists
what makes the sweep untrustworthy, and in fail mode the session ends non-zero on
it: fewer workers reported than started, fewer tests recorded than collected, a
guard error, Python children spawned but none seen by the shim, and a storage
touch AFTER its test finished (a thread or child the test leaked: the test itself
already passed, so the session carries the finding). The whole-run shape (some
test provisioned, some clean, some touch event) is asked only when
``NX_STORAGE_TOUCH_SWEEP=1``, which a full-suite run sets; a scoped run of three
provisioned tests is not vacuous for having no clean one. At start-up a canary
constructs each guarded class and checks the ``Thread``/``Popen`` patches, so a
run that finds nothing is a run that could have found something.

The guard is active under the default substrate and ``optin``; under ``none`` and
``per-test`` it installs nothing and records nothing. Two things are unconditional:
``_reset_vector_lease_cache`` (``http_vector_client`` caches a discovered lease
process-wide), and, with the guard active, the ``http_vector_client`` singleton
reset (a warm ``embedding_mode`` memo would hide a touch, a cold failed probe
would poison the next provisioned test).

A session started inside a test of a guarded one (a pytester run, a child pytest) is
``nested``: it keeps its own books and report directory and never writes into the
outer session's ``NX_STORAGE_TOUCH_REPORT_DIR``.

Durability: each process appends ONE JSON line per finished test to
``nx-storage-touch-tests-<run>-<pid>-<id>.jsonl`` under
``NX_STORAGE_TOUCH_REPORT_DIR`` (else its basetemp) and flushes, so a killed
worker loses at most the test it was running. The controller (or a serial run)
builds the report from those files: the ones it was handed by reporting
workers plus any left by workers that never reported (``recovered_files``).

Report (``nx-storage-touch-report.json``, path printed in the terminal
summary): ``counts``, ``findings`` (with origin, fixtures, deduped touches),
``modules`` (per file: tests / touched / thread_touched / peek_only / clean /
provisioned / exempt / skipped / failed / late_touches, ``failed_tests``,
``skipped_tests`` with reasons, ``setup_fixtures`` and the file defining each),
``soft_peeks``, ``skipped_modules`` (every test skipped), ``collect_skipped``
(module-level skips), ``late_child_touches``, ``late_thread_touches``,
``collected`` / ``recorded`` and ``vacuity``.

A pytest plugin, registered from ``tests/conftest.py``'s ``pytest_plugins``,
for the reason ``tests/_env_restore.py`` gives: a ``pytester`` session can load
it on its own, which is how ``tests/test_storage_touch_guard*.py`` prove it.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

SUBSTRATE_ENV = "NX_TEST_T2_SUBSTRATE"
OPTIN_VALUE = "optin"
#: The escape hatch of the 2026-10 default flip (nexus-j23ni): every test mints
#: its own tenant, as the suite did before. ``engine`` is the older spelling
#: tests/containers documents; it always meant the same thing.
PER_TEST_VALUE = "per-test"
PER_TEST_VALUES = frozenset({PER_TEST_VALUE, "engine"})
#: Values that provision nothing (``none``) or name the two tenant modes, and the
#: retired ones the conftest refuses by name. Anything else is a typo.
KNOWN_VALUES = frozenset({"none", OPTIN_VALUE}) | PER_TEST_VALUES
RETIRED_VALUES = frozenset({"sqlite"})
MODE_ENV = "NX_STORAGE_TOUCH_GUARD_MODE"
#: ``1`` asks the sweep-shape checks of a WHOLE run (some test provisioned, some
#: test clean, some touch event): a scoped run of three provisioned tests has no
#: clean one and is not vacuous. The census and the full-suite proof set it.
SWEEP_ENV = "NX_STORAGE_TOUCH_SWEEP"
REPORT_DIR_ENV = "NX_STORAGE_TOUCH_REPORT_DIR"
CHILD_LOG_ENV = "NX_STORAGE_TOUCH_CHILD_LOG"
GUARD_PID_ENV = "NX_STORAGE_TOUCH_GUARD_PID"
RUN_ID_ENV = "NX_STORAGE_TOUCH_RUN_ID"
EXEMPT_MARKER = "expects_unresolvable_endpoint"
TENANT_MARKER = "t2_tenant"
REPORT_NAME = "nx-storage-touch-report.json"
SHIM_DIR = Path(__file__).resolve().parent / "_storage_touch_shim"

#: (module, class) pairs whose construction is a storage touch. The shim's
#: ``_TARGETS`` is a copy; ``tests/test_storage_touch_guard_report.py`` keeps
#: the two equal.
GUARDED: tuple[tuple[str, str], ...] = (
    ("nexus.db.service_endpoint", "ServiceEndpointUnresolvableError"),
    ("nexus.db.service_endpoint", "ServiceTokenUnresolvableError"),
    ("nexus.db.t1", "T1ServerNotFoundError"),
    ("nexus.db.managed_endpoint", "ManagedServiceUnreachable"),
    ("nexus.db.managed_endpoint", "ManagedServiceIncompatible"),
)
_PEEK_CREDENTIALS = frozenset({"service_url", "service_token"})

#: Variables an outer guard exports; one already present at configure time means
#: this session runs inside a guarded one.
_INHERITED_ENV = (RUN_ID_ENV, GUARD_PID_ENV, CHILD_LOG_ENV)

_WORKEROUTPUT_KEY = "nx_storage_touch_report"
_JSONL_GLOB = "nx-storage-touch-tests-{run}-*.jsonl"
_STACK_FRAMES = 6
_SUMMARY_LINES = 25
_MAX_SIGNATURES = 8  # per test: distinct (phase, fixture, stack) entries kept
_MAX_PEEK_KEYS = 32  # per test: distinct (kind, site, phase, origin) soft peeks kept
_OWNER_ATTR = "_nx_storage_touch_owner"  # set on every Thread: the test running when it was created
#: src/nexus files that only resolve the endpoint on a caller's behalf: a soft
#: peek is attributed to the first src/nexus frame OUTSIDE them.
_PEEK_HELPER_FILES = ("db/service_endpoint.py", "db/http_vector_client.py", "config.py")


def substrate_value() -> str | None:
    """``NX_TEST_T2_SUBSTRATE`` as set, or None when unset or empty (the default)."""
    return os.environ.get(SUBSTRATE_ENV) or None


def optin_selected() -> bool:
    """True when this session runs the opt-in tenant mode: the DEFAULT (variable
    unset) and an explicit ``optin``. ``none`` provisions nothing and
    ``per-test`` is the escape hatch back to a tenant for every test; the guard
    is inert under both."""
    return substrate_value() in (None, OPTIN_VALUE)


def per_test_selected() -> bool:
    """True when every test mints its own tenant (the pre-flip behaviour)."""
    return substrate_value() in PER_TEST_VALUES


class StorageTouchError(AssertionError):
    """A test reached storage without a tenant (or kept a stale exemption)."""


@dataclass
class Finding:
    nodeid: str
    kind: str  # "unprovisioned_touch" | "thread_touch" | "stale_exemption"
    phases: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    origin: str | None = None
    count: int = 0
    dropped_events: int = 0
    stack: list[str] = field(default_factory=list)
    touches: list[dict] = field(default_factory=list)


@dataclass
class Counts:
    tests: int = 0
    provisioned: int = 0
    exempt: int = 0
    touched_unprovisioned: int = 0
    thread_touched: int = 0
    peek_only: int = 0
    clean_unprovisioned: int = 0
    stale_exemptions: int = 0
    touch_events: int = 0
    skipped: int = 0
    failed: int = 0
    foreign_thread_events: int = 0
    children_seen: int = 0
    children_spawned: int = 0  # Python children the parent started; seen < spawned means the shim missed some
    guard_errors: int = 0


# ── frames ───────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve()
_TESTS_PREFIX = str(_HERE.parent) + os.sep


@functools.lru_cache(maxsize=4096)
def _real(filename: str) -> str:
    return os.path.realpath(filename)


@functools.lru_cache(maxsize=1)
def _nexus_prefix() -> str:
    import nexus

    return str(Path(nexus.__file__).resolve().parent) + os.sep


Frame = tuple[str, int, str]  # (filename, lineno, function)


def _fmt(frame: Frame) -> str:
    path, lineno, name = frame
    return f"{_rel(path)}:{lineno} in {name}"


def _rel(path: str) -> str:
    """``path`` relative to the cwd when it can be. A deleted cwd (``Path.cwd()``
    raises ``FileNotFoundError``) or an unrelated path leaves it as it is: the
    guard must never raise into the code under test."""
    with contextlib.suppress(ValueError, OSError):
        return str(Path(path).relative_to(Path.cwd()))
    return path


def _frames() -> list[Frame]:
    """The current stack, outermost first, this module's own frames removed.
    Walks frame objects directly: ``traceback.extract_stack`` reads source
    lines, which is too slow for a hook that may fire thousands of times."""
    out: list[Frame] = []
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if _real(code.co_filename) != str(_HERE):
            out.append((code.co_filename, frame.f_lineno, code.co_name))
        frame = frame.f_back
    out.reverse()
    return out


def _short_stack(frames: list[Frame]) -> list[str]:
    return [_fmt(f) for f in frames[-_STACK_FRAMES:]]


def _origin(frames: list[Frame], item_path: str | None) -> str | None:
    """The outermost frame that is test-side: under ``tests/`` or in the item's
    own file. Pytest hook implementations (``pytest_*``) are skipped: they sit
    above every test and say nothing about which test code caused the touch."""
    # Only the frames below the innermost runtest protocol belong to this test:
    # a nested pytester session runs inside an outer test, whose frames sit above.
    start = max((i for i, f in enumerate(frames) if f[2] == "pytest_runtest_protocol"), default=0)
    for f in frames[start:]:
        if f[2].startswith("pytest_"):
            continue
        real = _real(f[0])
        if real.startswith(_TESTS_PREFIX) or real == item_path:
            return _fmt(f)
    return None


# ── class-level patches ──────────────────────────────────────────────────────

_guards: list[Guard] = []  # innermost last; a nested pytester session pushes
_patch_lock = threading.Lock()
_patch_refs = 0
_originals: dict[type, tuple[bool, object]] = {}
_peek_wrappers: dict[str, tuple[object, object]] = {}  # name -> (original, wrapper)


def _guarded_classes() -> tuple[type, ...]:
    import importlib

    return tuple(getattr(importlib.import_module(mod), name) for mod, name in GUARDED)


def _count_error(guard: "Guard | None") -> None:
    if guard is not None:
        guard.guard_errors += 1


def _record_touch(error_type: str) -> None:
    """Called from inside a guarded error's ``__init__``: whatever goes wrong
    in here is counted (``guard_errors``) and swallowed, never raised into the
    code under test."""
    guard = _guards[-1] if _guards else None
    if guard is None:
        return
    if guard.canary_active:  # the start-up self-test: counted, never recorded
        guard.canary_hits += 1
        return
    try:
        guard.record(error_type)
    except Exception:  # noqa: BLE001 — a recorder must never break the code under test
        _count_error(guard)


def _make_patched(cls: type, original):
    def __init__(self, *args, **kwargs):  # noqa: N807 — replacing __init__ by name
        original(self, *args, **kwargs)
        _record_touch(type(self).__name__)

    __init__.__qualname__ = f"{cls.__qualname__}.__init__"
    __init__._nx_storage_touch_patched = True  # type: ignore[attr-defined]
    return __init__


def _peek_consumer(frames: list[Frame], item_path: str | None) -> Frame | None:
    """The first src/nexus frame, innermost first, outside the endpoint helpers:
    the code that WANTED the endpoint. Reaching test-side code first means a test
    called the helper itself, which is not a src/nexus peek."""
    prefix = _nexus_prefix()
    helpers = {os.path.join(prefix, rel) for rel in _PEEK_HELPER_FILES}
    for f in reversed(frames):
        real = _real(f[0])
        if real.startswith(_TESTS_PREFIX) or real == item_path:
            return None
        if real.startswith(prefix) and real not in helpers:
            return f
    return None


def _record_peek(kind: str) -> None:
    guard = _guards[-1] if _guards else None
    if guard is None:
        return
    try:
        if not guard.wants_peek():  # provisioned / off-thread / between tests: no stack walk, no formatting
            return
        frames = _frames()
        consumer = _peek_consumer(frames, guard._item_path())
        if consumer is not None:
            guard.record_peek(kind, _fmt(consumer), _origin(frames, guard._item_path()))
    except Exception:  # noqa: BLE001 — see _record_touch
        _count_error(guard)


def _make_peek_wrappers() -> dict[str, tuple[object, object]]:
    from nexus import config
    from nexus.db import service_endpoint

    original_credential = config.get_credential
    original_lease = service_endpoint.discover_lease

    @functools.wraps(original_credential)
    def get_credential(*args, **kwargs):
        name = args[0] if args else kwargs.get("name")
        if isinstance(name, str) and name in _PEEK_CREDENTIALS:
            _record_peek(f"get_credential:{name}")
        return original_credential(*args, **kwargs)

    @functools.wraps(original_lease)
    def discover_lease(*args, **kwargs):
        _record_peek("discover_lease")
        return original_lease(*args, **kwargs)

    for wrapper in (get_credential, discover_lease):
        wrapper._nx_storage_touch_peek = True  # type: ignore[attr-defined]
    return {
        "get_credential": (original_credential, get_credential),
        "discover_lease": (original_lease, discover_lease),
    }


def _rebind(old: object, new: object) -> None:
    """Swap every module-level binding of ``old`` for ``new``. ``from x import f``
    copies the function object, so patching the defining module alone would
    miss those, whatever name (or alias) they bound it under."""
    for module in list(sys.modules.values()):
        try:
            namespace = vars(module)
            items = list(namespace.items())
        except (TypeError, RuntimeError):
            continue
        for key, value in items:
            if value is old:
                with contextlib.suppress(Exception):
                    namespace[key] = new


_thread_init: list[object] = []  # [the original Thread.__init__] while installed
_popen_init: list[object] = []  # [the original subprocess.Popen.__init__] while installed
_PYTHON_LAUNCHERS = frozenset({"nx", "uv", "pytest", "py.test", "pip", "pipx"})


def _is_python_command(cmd: object) -> bool:
    """True when ``cmd`` (a Popen ``args``) starts a Python process: the
    interpreter itself, or one of the console scripts the suite spawns. A shell
    string is not parsed (the first word of ``sh -c`` says nothing), so those
    children are not counted as spawned."""
    if isinstance(cmd, (str, bytes)) or not cmd:
        return False
    try:
        first = os.fspath(cmd[0])  # type: ignore[index]
    except (TypeError, IndexError, KeyError):
        return False
    if isinstance(first, bytes):
        first = os.fsdecode(first)
    base = os.path.basename(first).lower()
    return base.startswith("python") or base in _PYTHON_LAUNCHERS or _real(first) == _real(sys.executable)


def _make_popen_init(original):
    @functools.wraps(original)
    def __init__(self, *args, **kwargs):  # noqa: N807 — replacing __init__ by name
        try:
            guard = _guards[-1] if _guards else None
            if guard is not None and guard.item is not None and not guard.canary_active:
                cmd = args[0] if args else kwargs.get("args")
                if _is_python_command(cmd):
                    with guard._lock:
                        guard.spawned += 1
        except Exception:  # noqa: BLE001 — see _record_touch
            pass
        original(self, *args, **kwargs)

    __init__._nx_storage_touch_popen = True  # type: ignore[attr-defined]
    return __init__


def _make_thread_init(original):
    @functools.wraps(original)
    def __init__(self, *args, **kwargs):  # noqa: N807 — replacing __init__ by name
        original(self, *args, **kwargs)
        try:
            guard = _guards[-1] if _guards else None
            item = guard.item if guard is not None else None
            setattr(self, _OWNER_ATTR, item.nodeid if item is not None else None)
        except Exception:  # noqa: BLE001 — see _record_touch
            pass

    __init__._nx_storage_touch_thread = True  # type: ignore[attr-defined]
    return __init__


def _install() -> None:
    global _patch_refs
    with _patch_lock:
        if _patch_refs == 0:
            for cls in _guarded_classes():
                own = "__init__" in cls.__dict__
                original = cls.__init__
                _originals[cls] = (own, cls.__dict__.get("__init__"))
                cls.__init__ = _make_patched(cls, original)
            _peek_wrappers.update(_make_peek_wrappers())
            for original, wrapper in _peek_wrappers.values():
                _rebind(original, wrapper)
            _thread_init.append(threading.Thread.__init__)
            threading.Thread.__init__ = _make_thread_init(threading.Thread.__init__)  # type: ignore[method-assign]
            _popen_init.append(subprocess.Popen.__init__)
            subprocess.Popen.__init__ = _make_popen_init(subprocess.Popen.__init__)  # type: ignore[method-assign]
        _patch_refs += 1


def _uninstall() -> None:
    global _patch_refs
    with _patch_lock:
        _patch_refs -= 1
        if _patch_refs == 0:
            for cls, (own, saved) in _originals.items():
                if own:
                    cls.__init__ = saved
                else:
                    del cls.__init__
            _originals.clear()
            for original, wrapper in _peek_wrappers.values():
                _rebind(wrapper, original)
            _peek_wrappers.clear()
            if _thread_init:
                threading.Thread.__init__ = _thread_init.pop()  # type: ignore[method-assign]
            if _popen_init:
                subprocess.Popen.__init__ = _popen_init.pop()  # type: ignore[method-assign]


# ── records and roll-up ──────────────────────────────────────────────────────


def _unprovisioned(rec: dict) -> list[dict]:
    return [t for t in rec["touches"] if not t["provisioned"]]


def _finding_of(rec: dict) -> Finding | None:
    """The finding a test record amounts to, or None. One definition for the
    in-process teardown check and the merged report.

    A test whose only unprovisioned touches were built on a thread other than
    the test's is a ``thread_touch`` finding: still a finding (the code under
    test reached storage without a tenant), reported as its own kind."""
    if rec.get("stale"):
        return Finding(nodeid=rec["nodeid"], kind="stale_exemption")
    if not rec["unprov_events"] or rec["exempt"]:
        return None
    bad = _unprovisioned(rec)
    hard = rec["unprov_events"] - rec.get("thread_unprov_events", 0)
    return Finding(
        nodeid=rec["nodeid"],
        kind="unprovisioned_touch" if hard else "thread_touch",
        phases=sorted({t["phase"] for t in bad}),
        error_types=sorted({t["error_type"] for t in bad}),
        fixtures=sorted({t["fixture"] for t in bad if t["fixture"]}),
        origin=next((t["origin"] for t in bad if t["origin"]), None),
        count=rec["unprov_events"],
        dropped_events=rec["dropped_events"],
        stack=bad[0]["stack"] if bad else [],
        touches=bad,
    )


def _new_module() -> dict:
    return {
        "tests": 0, "touched": 0, "thread_touched": 0, "peek_only": 0, "clean": 0,
        "provisioned": 0, "exempt": 0, "skipped": 0, "failed": 0, "late_touches": 0,
        "setup_fixtures": {}, "setup_fixture_files": {}, "failed_tests": [], "skipped_tests": [],
    }


def _owning_module(nodeid: str) -> str | None:
    head = nodeid.split("::")[0]
    return head if nodeid and not nodeid.startswith("<") and head else None


def _rollup(records: list[dict]) -> dict:
    counts = Counts()
    modules: dict[str, dict] = {}
    findings: list[Finding] = []
    peeks: dict[tuple[str, str], dict] = {}
    collect_skipped: set[str] = set()
    late_child: list[dict] = []
    late_thread: list[dict] = []
    xfail_touches: list[str] = []
    for rec in records:
        kind = rec["k"]
        if kind == "collect_skip":
            collect_skipped.add(rec["nodeid"])
            continue
        if kind == "late_child":
            late_child.append(rec)
            continue
        if kind == "late_thread":
            late_thread.append(rec)
            continue
        if kind == "late_child_start":
            counts.children_seen += rec.get("n", 1)
            continue
        if kind == "guard_errors":
            counts.guard_errors += rec["n"]
            continue
        module = rec["nodeid"].split("::")[0]
        m = modules.setdefault(module, _new_module())
        finding = _finding_of(rec)
        skipped = rec["outcome"] in ("skipped", "xfailed")
        failed = rec["outcome"] == "failed"
        touched = finding is not None and finding.kind == "unprovisioned_touch"
        thread_touched = finding is not None and finding.kind == "thread_touch"
        peeked = bool(rec.get("soft_peeks"))
        unexamined = not rec["provisioned"] and not rec["events"] and not rec["exempt"] and not skipped and not rec.get("stale")
        peek_only = unexamined and peeked
        clean = unexamined and not peeked
        counts.tests += 1
        counts.touch_events += rec["events"]
        counts.foreign_thread_events += rec.get("thread_events", 0)
        counts.children_seen += rec.get("children", 0)
        counts.children_spawned += rec.get("spawned", 0)
        counts.guard_errors += rec.get("guard_errors", 0)
        counts.provisioned += rec["provisioned"]
        counts.exempt += rec["exempt"]
        counts.skipped += skipped
        counts.failed += failed
        counts.touched_unprovisioned += touched
        counts.thread_touched += thread_touched
        counts.peek_only += peek_only
        counts.stale_exemptions += bool(finding and finding.kind == "stale_exemption")
        counts.clean_unprovisioned += clean
        m["tests"] += 1
        m["provisioned"] += rec["provisioned"]
        m["exempt"] += rec["exempt"]
        m["skipped"] += skipped
        m["failed"] += failed
        m["touched"] += touched
        m["thread_touched"] += thread_touched
        m["peek_only"] += peek_only
        m["clean"] += clean
        if failed:
            m["failed_tests"].append(rec["nodeid"])
        if skipped:
            m["skipped_tests"].append({"nodeid": rec["nodeid"], "reason": rec.get("skip_reason")})
        if touched or thread_touched:
            for t in _unprovisioned(rec):
                if t["phase"] != "setup":
                    continue
                key = t["fixture"] or "<hook>"
                m["setup_fixtures"][key] = m["setup_fixtures"].get(key, 0) + 1
                if t.get("fixture_file"):
                    files = m["setup_fixture_files"].setdefault(key, [])
                    if t["fixture_file"] not in files:
                        files.append(t["fixture_file"])
        if finding is not None:
            findings.append(finding)
            if rec["outcome"] == "xfailed":
                xfail_touches.append(rec["nodeid"])
        for p in rec.get("soft_peeks", ()):
            entry = peeks.setdefault((p["kind"], p["site"]), {
                "kind": p["kind"], "site": p["site"], "count": 0, "tests": 0, "example": rec["nodeid"],
                "origins": [], "peek_only_tests": [],
            })
            entry["count"] += p["count"]
            if rec["nodeid"] not in entry.setdefault("_seen", set()):
                entry["_seen"].add(rec["nodeid"])
                entry["tests"] += 1
                if peek_only:
                    entry["peek_only_tests"].append(rec["nodeid"])
            if p.get("origin") and p["origin"] not in entry["origins"]:
                entry["origins"].append(p["origin"])
    for entry in peeks.values():
        entry.pop("_seen", None)
        entry["origins"] = entry["origins"][:10]
    for rec in (*late_child, *late_thread):
        owner = _owning_module(rec["nodeid"])
        if owner is not None:
            modules.setdefault(owner, _new_module())["late_touches"] += rec.get("n", 1)
    for m in modules.values():
        m["failed_tests"].sort()
        m["skipped_tests"].sort(key=lambda s: s["nodeid"])
        m["setup_fixture_files"] = {k: sorted(v) for k, v in sorted(m["setup_fixture_files"].items())}
    findings.sort(key=lambda f: (f.kind, f.nodeid))
    return {
        "counts": asdict(counts),
        "findings": [asdict(f) for f in findings],
        "modules": dict(sorted(modules.items())),
        "soft_peeks": sorted(peeks.values(), key=lambda p: (-p["tests"], p["kind"], p["site"])),
        "skipped_modules": sorted(k for k, v in modules.items() if v["tests"] and v["skipped"] == v["tests"]),
        "collect_skipped": sorted(collect_skipped),
        "late_child_touches": late_child,
        "late_thread_touches": late_thread,
        "xfail_touches": sorted(xfail_touches),
    }


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut short by a kill: everything before it is intact
    return out


def _basetemp(config: pytest.Config) -> Path:
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        return factory.getbasetemp()
    import tempfile

    return Path(tempfile.mkdtemp(prefix="nx-storage-touch-"))


# ── per-session state ────────────────────────────────────────────────────────


class Guard:
    """One session's recorder. Registered as a plugin so its hooks run."""

    def __init__(self, mode: str, config: pytest.Config) -> None:
        self.mode = mode
        self.config = config
        self.is_worker = hasattr(config, "workerinput")
        #: A session started INSIDE a test of another guarded session (a pytester
        #: run, a child pytest): the outer one's exports are in its environment
        #: and it is not one of that session's xdist workers. It keeps its own
        #: books and its own report directory, so it can never overwrite the
        #: outer session's report (the census of 2026-10-18 lost its merged
        #: report to exactly this).
        self.nested = not self.is_worker and any(os.environ.get(v) for v in _INHERITED_ENV)
        self.sweep = os.environ.get(SWEEP_ENV) == "1"
        self.canary_active = False
        self.canary_hits = 0
        self.guard_id = uuid.uuid4().hex[:8]
        self.run_id = (os.environ.get(RUN_ID_ENV) if self.is_worker else None) or uuid.uuid4().hex[:8]
        self._saved_env: dict[str, str | None] = {}
        self._dir: Path | None = None
        self._jsonl: Path | None = None
        self._jsonl_fh = None
        self._child_log: Path | None = None
        self._child_offset = 0
        self._lock = threading.RLock()  # record() runs on whatever thread built the error
        self.fixture_stack: list[tuple[str, object]] = []  # (argname, fixturedef)
        self._fixture_files: dict[int, str | None] = {}
        self.collect_skips: set[str] = set()
        self.workers_started = 0
        self.reported: list[dict] = []
        self.guard_errors = 0  # internal errors swallowed; written with the next test record
        self._late_threads: dict[tuple, dict] = {}
        self.collected = 0
        self._begin_state()
        self._verdict: list[str] = []
        self._merged: dict | None = None

    # per-test state -------------------------------------------------------
    def _begin_state(self) -> None:
        self.item: pytest.Item | None = None
        self.phase = ""
        self.thread = 0
        self.tenant_depth = 0
        self.ever_provisioned = False
        self.call_passed = False
        self.outcome = "passed"
        self.skip_reason: str | None = None
        self.events = 0
        self.unprov_events = 0
        self.thread_events = 0
        self.thread_unprov = 0
        self.children = 0
        self.spawned = 0
        self.sigs: dict[tuple, dict] = {}
        self.dropped_events = 0
        self.peeks: dict[tuple[str, str, str, str | None], int] = {}
        self.dropped_peeks = 0
        self.fixture_stack.clear()

    def _item_path(self) -> str | None:
        path = getattr(self.item, "path", None)
        return _real(str(path)) if path is not None else None

    # recording ------------------------------------------------------------
    def _live(self) -> bool:
        """Attributable to the running test: an item, on that test's thread."""
        return self.item is not None and threading.get_ident() == self.thread

    def wants_peek(self) -> bool:
        return self._live() and self.tenant_depth == 0

    def _fixture_file(self, fixturedef: object) -> str | None:
        key = id(fixturedef)
        if key not in self._fixture_files:
            try:
                func = inspect.unwrap(fixturedef.func)  # type: ignore[attr-defined]
                self._fixture_files[key] = _rel(func.__code__.co_filename)
            except Exception:  # noqa: BLE001 — a fixture without a plain function
                self._fixture_files[key] = None
        return self._fixture_files[key]

    def _add_sig(self, *, source: str, phase: str, error_type: str, fixture: str | None,
                 fixture_file: str | None, provisioned: bool, stack: list[str], origin, n: int = 1) -> None:
        """Count ``n`` events and fold them into the per-test signature table:
        identical (phase, type, fixture, stack) entries collapse, and past
        ``_MAX_SIGNATURES`` distinct ones the events are only counted."""
        self.events += n
        if not provisioned:
            self.unprov_events += n
        if source == "thread":
            self.thread_events += n
            if not provisioned:
                self.thread_unprov += n
        key = (phase, error_type, fixture, provisioned, source, tuple(stack))
        entry = self.sigs.get(key)
        if entry is None:
            if len(self.sigs) >= _MAX_SIGNATURES:
                self.dropped_events += n
                return
            entry = self.sigs[key] = {
                "phase": phase, "error_type": error_type, "fixture": fixture, "fixture_file": fixture_file,
                "provisioned": provisioned, "source": source, "count": 0,
                "origin": origin() if callable(origin) else origin, "stack": stack,
            }
        entry["count"] += n

    def _thread_owner(self) -> str | None:
        """The nodeid of the test running when the current thread was created
        (``Thread.__init__`` is patched to record it), or None when it is not
        known: created outside any test, or not through ``threading.Thread``."""
        return getattr(threading.current_thread(), _OWNER_ATTR, None)

    def _late_thread(self, owner: str, running: str | None, error_type: str) -> None:
        """A guarded error built on a thread that a FINISHED test started. Its
        owner already has a record, so it is listed on its own, naming both."""
        stack = _short_stack(_frames())
        sig = (owner, error_type, tuple(stack))
        known = self._late_threads.get(sig)
        if known is not None:
            known["n"] += 1
            return
        rec = {
            "k": "late_thread", "nodeid": owner, "running": running, "phase": self.phase,
            "error_type": error_type, "thread": threading.current_thread().name, "stack": stack, "n": 1,
        }
        self._late_threads[sig] = rec
        self._write(rec)

    def record(self, error_type: str) -> None:
        with self._lock:
            item = self.item
            on_test_thread = threading.get_ident() == self.thread
            if item is None or not on_test_thread:
                owner = None if on_test_thread else self._thread_owner()
                if owner is not None and owner != (item.nodeid if item is not None else None):
                    self._late_thread(owner, item.nodeid if item is not None else None, error_type)
                    return
                if item is None:
                    return  # collection / session scope: not attributable to a test
            # A thread with no recorded owner, or one this test started, is the
            # running test's: its touch is a finding like any other.
            source = "parent" if on_test_thread else "thread"
            frames = _frames()
            fixture = ffile = None
            if source == "parent" and self.phase == "setup" and self.fixture_stack:
                fixture, fixturedef = self.fixture_stack[-1]
                ffile = self._fixture_file(fixturedef)
            path = self._item_path()
            self._add_sig(
                source=source, phase=self.phase, error_type=error_type, fixture=fixture,
                fixture_file=ffile, provisioned=self.tenant_depth > 0, stack=_short_stack(frames),
                origin=lambda: _origin(frames, path),
            )

    def record_peek(self, kind: str, site: str, origin: str | None) -> None:
        key = (kind, site, self.phase, origin)
        if key not in self.peeks and len(self.peeks) >= _MAX_PEEK_KEYS:
            self.dropped_peeks += 1
            return
        self.peeks[key] = self.peeks.get(key, 0) + 1

    # child processes ------------------------------------------------------
    def _drain_children(self) -> list[dict]:
        """New complete lines from the child log, as parsed dicts."""
        if self._child_log is None or not self._child_log.exists():
            return []
        with self._child_log.open("rb") as fh:
            fh.seek(self._child_offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1
        self._child_offset += end
        out: list[dict] = []
        for raw in data[:end].splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(raw))
        return out

    def _late_child(self, line: dict) -> None:
        """A child line whose test is not the one just finished: written on its
        own, uncapped. Peeks and drop tallies are advisory and skipped."""
        nodeid, phase = _split_current_test(line.get("current_test") or "")
        kind = line.get("k", "touch")
        if kind == "child_start":
            self._write({"k": "late_child_start", "nodeid": nodeid or "<no test running>", "n": 1})
        elif kind == "touch":
            self._write({
                "k": "late_child", "nodeid": nodeid or "<no test running>", "phase": phase,
                "error_type": line.get("error_type"), "pid": line.get("pid"),
                "stack": [_relative(s) for s in line.get("stack", ())], "n": line.get("n", 1),
            })

    def _fold_children(self, item: pytest.Item) -> None:
        for line in self._drain_children():
            nodeid, phase = _split_current_test(line.get("current_test") or "")
            if nodeid != item.nodeid:
                self._late_child(line)
                continue
            kind = line.get("k", "touch")
            if kind == "child_start":
                self.children += 1
            elif kind == "touch":
                self._add_sig(
                    source="child", phase=phase, error_type=line.get("error_type"), fixture=None,
                    fixture_file=None, provisioned=self.ever_provisioned,
                    stack=[_relative(s) for s in line.get("stack", ())], origin=None, n=line.get("n", 1),
                )
            elif kind == "peek" and not self.ever_provisioned:
                key = (line["peek_kind"], _relative(line["site"]), phase, None)
                if key in self.peeks or len(self.peeks) < _MAX_PEEK_KEYS:
                    self.peeks[key] = self.peeks.get(key, 0) + line.get("n", 1)
                else:
                    self.dropped_peeks += 1
            elif kind == "dropped":
                self.dropped_events += line.get("n", 1)

    # lifecycle ------------------------------------------------------------
    def _ensure_paths(self) -> None:
        if self._dir is not None:
            return
        configured = None if self.nested else os.environ.get(REPORT_DIR_ENV)
        self._dir = Path(configured) if configured else _basetemp(self.config)
        self._dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.run_id}-{os.getpid()}-{self.guard_id}"
        self._jsonl = self._dir / f"nx-storage-touch-tests-{stem}.jsonl"
        self._child_log = self._dir / f"nx-storage-touch-children-{stem}.jsonl"
        self._export_env()

    def _set_env(self, name: str, value: str | None) -> None:
        self._saved_env.setdefault(name, os.environ.get(name))
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _export_env(self) -> None:
        self._set_env(RUN_ID_ENV, self.run_id)
        self._set_env(GUARD_PID_ENV, str(os.getpid()))
        self._set_env(CHILD_LOG_ENV, str(self._child_log))
        if self.nested:
            # this session's own workers must find THIS controller's directory, not
            # the outer operator's: they fall back to their basetemp under ours
            self._set_env(REPORT_DIR_ENV, None)
        existing = os.environ.get("PYTHONPATH")
        self._set_env("PYTHONPATH", os.pathsep.join([str(SHIM_DIR), *([existing] if existing else [])]))

    def restore_env(self) -> None:
        for name, value in reversed(list(self._saved_env.items())):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._saved_env.clear()
        if self._jsonl_fh is not None:
            self._jsonl_fh.close()
            self._jsonl_fh = None

    def _write(self, rec: dict) -> None:
        with self._lock:
            self._ensure_paths()
            if self._jsonl_fh is None:
                self._jsonl_fh = self._jsonl.open("a", encoding="utf-8")  # type: ignore[union-attr]
            self._jsonl_fh.write(json.dumps(rec) + "\n")
            self._jsonl_fh.flush()

    def _begin(self, item: pytest.Item) -> None:
        self._ensure_paths()
        with self._lock:
            self._begin_state()
            self.item = item
            self.phase = "setup"
            self.thread = threading.get_ident()

    def _finalize(self, item: pytest.Item, *, raise_on_finding: bool) -> None:
        # the literal name is what tests/test_marker_selection_coverage.py looks for
        exempt = item.get_closest_marker("expects_unresolvable_endpoint") is not None
        with self._lock:
            self._fold_children(item)
            errors, self.guard_errors = self.guard_errors, 0
            rec = {
                "k": "test", "nodeid": item.nodeid, "outcome": self.outcome, "skip_reason": self.skip_reason,
                "provisioned": self.ever_provisioned, "exempt": exempt,
                "events": self.events, "unprov_events": self.unprov_events,
                "thread_events": self.thread_events, "thread_unprov_events": self.thread_unprov,
                "children": self.children, "spawned": self.spawned, "guard_errors": errors,
                "dropped_events": self.dropped_events, "dropped_peeks": self.dropped_peeks,
                "touches": list(self.sigs.values()),
                "soft_peeks": [
                    {"kind": k, "site": s, "phase": ph, "origin": o, "count": n}
                    for (k, s, ph, o), n in sorted(self.peeks.items(), key=lambda kv: tuple(map(str, kv[0])))
                ],
                "stale": bool(exempt and not self.events and self.call_passed),
            }
            self._write(rec)
            finding = _finding_of(rec)
            self._begin_state()
        if finding is not None and raise_on_finding and self.mode == "fail":
            raise StorageTouchError(_describe(finding))

    def _close_out(self) -> None:
        """Everything still unwritten at the end of this process's session."""
        with self._lock:
            self._ensure_paths()
            for line in self._drain_children():  # stragglers the last test did not see
                self._late_child(line)
            for rec in self._late_threads.values():
                if rec["n"] > 1:
                    self._write({**rec, "n": rec["n"] - 1, "tally": True})
            if self.guard_errors:
                self._write({"k": "guard_errors", "n": self.guard_errors})
                self.guard_errors = 0
            if self._jsonl_fh is not None:
                self._jsonl_fh.flush()

    # hooks ------------------------------------------------------------------
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        self._begin(item)
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.fixture_stack.append((fixturedef.argname, fixturedef))
        try:
            return (yield)
        finally:
            if self.fixture_stack:
                self.fixture_stack.pop()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        self.phase = "call"
        result = yield
        self.call_passed = True
        return result

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.phase = "teardown"
        try:
            result = yield
        except BaseException:
            self._finalize(item, raise_on_finding=False)
            raise
        self._finalize(item, raise_on_finding=True)
        return result

    def pytest_runtest_logreport(self, report):
        if self.item is None or report.when not in ("setup", "call"):
            return
        if report.failed:
            self.outcome = "failed"
        elif report.skipped and self.outcome != "failed":
            self.outcome = "xfailed" if hasattr(report, "wasxfail") else "skipped"
            self.skip_reason = _skip_reason(report)

    def pytest_collectreport(self, report):
        if report.skipped and report.nodeid and report.nodeid not in self.collect_skips:
            self.collect_skips.add(report.nodeid)
            self._write({"k": "collect_skip", "nodeid": report.nodeid})

    # xdist ------------------------------------------------------------------
    @pytest.hookimpl(optionalhook=True)  # xdist's hooks; absent under -p no:xdist
    def pytest_configure_node(self, node):
        self.workers_started += 1

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        payload = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
        if payload:
            self.reported.append(payload)

    # report -----------------------------------------------------------------
    def merged(self) -> dict:
        if self._merged is not None:
            return self._merged
        self._ensure_paths()
        if self._jsonl_fh is not None:
            self._jsonl_fh.flush()
        own = [self._jsonl] if self._jsonl and self._jsonl.exists() else []
        known = {str(p) for p in own} | {p["jsonl"] for p in self.reported}
        scanned = sorted((self._dir or Path(".")).rglob(_JSONL_GLOB.format(run=self.run_id)))
        recovered = [p for p in scanned if str(p) not in known]
        records: list[dict] = []
        for path in [*own, *(Path(p["jsonl"]) for p in self.reported if Path(p["jsonl"]).exists()), *recovered]:
            records.extend(_read_jsonl(path))
        report = _rollup(records)
        report.update({
            "mode": self.mode,
            "workers_started": self.workers_started,
            "workers": len(self.reported),
            "recovered_files": [str(p) for p in recovered],
            "collected": self.collected,
            "recorded": report["counts"]["tests"],
            "vacuity": [],
        })
        self._merged = report
        return report

    def _vacuity(self, report: dict) -> list[str]:
        """Everything that makes the sweep's verdict untrustworthy or the run
        unclean, as lines. Always checked: every worker reported, the recorder
        raised nothing, every collected test was recorded, children that were
        spawned reached the shim, and nothing touched storage after its test
        finished. The shape of the WHOLE run (some test provisioned, some clean,
        some touch event) is asked only when ``NX_STORAGE_TOUCH_SWEEP=1``: a
        scoped run of three provisioned tests is not vacuous for having no
        clean one, and the start-up canary already proved the recorder fires."""
        c = report["counts"]
        problems = []
        if self.sweep:
            problems += [
                f"vacuous sweep: {name}=0 (must be > 0)"
                for name in ("provisioned", "clean_unprovisioned", "touch_events")
                if not c[name]
            ]
        if report["workers"] != report["workers_started"]:
            problems.append(
                f"vacuous sweep: workers reported {report['workers']} of {report['workers_started']} started"
            )
        if c["guard_errors"]:
            problems.append(
                f"guard failure: {c['guard_errors']} internal error(s) swallowed, so the census may be incomplete"
            )
        if report["collected"] and report["recorded"] < report["collected"]:
            problems.append(
                f"vacuous sweep: recorded {report['recorded']} of {report['collected']} collected tests"
            )
        if c["children_spawned"] and not c["children_seen"]:
            problems.append(
                f"vacuous sweep: {c['children_spawned']} Python child(ren) were spawned but the shim saw none "
                "(PYTHONPATH scrubbed, or the shim could not load): child touches are unexamined"
            )
        if report["xfail_touches"]:
            shown = ", ".join(report["xfail_touches"][:5])
            problems.append(
                f"storage touch swallowed by xfail: {len(report['xfail_touches'])} xfailed test(s) reached storage "
                f"without a tenant, and xfail turns the teardown failure into an expected one: {shown}. "
                "Mark them t2_tenant so they xfail for their own reason."
            )
        late = report["late_child_touches"] + report["late_thread_touches"]
        if late:
            owners = sorted({r["nodeid"] for r in late})
            shown = ", ".join(owners[:5]) + (f", ... {len(owners) - 5} more" if len(owners) > 5 else "")
            problems.append(
                f"late storage touch: {sum(r.get('n', 1) for r in late)} construction(s) after their test finished, "
                f"from {len(owners)} test(s) that leaked a thread or child: {shown}. "
                "Join the thread or wait for the child inside the test."
            )
        return problems

    def run_canary(self) -> None:
        """Prove, at session start, that the patches are in and the recorder is
        reachable, so a run with no touch to report is not a run that could not
        have seen one. Raises ``pytest.UsageError`` when it cannot see itself."""
        self.canary_hits = 0
        self.canary_active = True
        classes = _guarded_classes()
        # This process hosts a guard from here on: the child shim (this session may
        # itself be a child of a guarded one) must not log the canary as a touch.
        saved_owner = os.environ.get(GUARD_PID_ENV)
        os.environ[GUARD_PID_ENV] = str(os.getpid())
        try:
            for cls in classes:
                with contextlib.suppress(Exception):
                    cls("storage-touch guard canary")
        finally:
            self.canary_active = False
            if saved_owner is None:
                os.environ.pop(GUARD_PID_ENV, None)
            else:
                os.environ[GUARD_PID_ENV] = saved_owner
        missing = []
        if self.canary_hits != len(classes):
            missing.append(f"{len(classes) - self.canary_hits} of {len(classes)} guarded error classes did not reach the recorder")
        if not getattr(threading.Thread.__init__, "_nx_storage_touch_thread", False):
            missing.append("threading.Thread.__init__ is not patched")
        if not getattr(subprocess.Popen.__init__, "_nx_storage_touch_popen", False):
            missing.append("subprocess.Popen.__init__ is not patched")
        if missing:
            raise pytest.UsageError("storage-touch guard cannot see itself: " + "; ".join(missing))

    def pytest_sessionfinish(self, session):
        config = session.config
        self._close_out()
        workeroutput = getattr(config, "workeroutput", None)
        if workeroutput is not None:
            workeroutput[_WORKEROUTPUT_KEY] = {"jsonl": str(self._jsonl), "pid": os.getpid()}
            return
        if config.option.collectonly:
            return
        self.collected = session.testscollected
        report = self.merged()
        report["collected"] = self.collected
        self._verdict = report["vacuity"] = self._vacuity(report)
        if self.mode == "fail" and self._verdict and not session.exitstatus:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if hasattr(config, "workerinput"):
            return
        report = self.merged()
        path = (self._dir or _basetemp(config)) / REPORT_NAME
        path.write_text(json.dumps(report, indent=1))
        c = report["counts"]
        late_child = sum(r.get("n", 1) for r in report["late_child_touches"])
        late_thread = sum(r.get("n", 1) for r in report["late_thread_touches"])
        tr = terminalreporter
        tr.section("storage-touch guard (nexus-j23ni)")
        tr.write_line(
            f"mode={report['mode']} tests={c['tests']} provisioned={c['provisioned']} "
            f"touched_unprovisioned={c['touched_unprovisioned']} thread_touched={c['thread_touched']} "
            f"peek_only={c['peek_only']} clean_unprovisioned={c['clean_unprovisioned']} exempt={c['exempt']} "
            f"stale_exemptions={c['stale_exemptions']} touch_events={c['touch_events']} "
            f"skipped={c['skipped']} failed={c['failed']} children_seen={c['children_seen']} children_spawned={c['children_spawned']} "
            f"late_child_touches={late_child} late_thread_touches={late_thread} "
            f"guard_errors={c['guard_errors']} collected={report['collected']} recorded={report['recorded']}"
        )
        worst = sorted(
            ((name, m) for name, m in report["modules"].items() if m["touched"] or m["thread_touched"]),
            key=lambda kv: -(kv[1]["touched"] + kv[1]["thread_touched"]),
        )
        for name, m in worst[:_SUMMARY_LINES]:
            fx = ",".join(f"{k}x{v}" for k, v in sorted(m["setup_fixtures"].items()))
            tr.write_line(
                f"  module {name}: touched={m['touched']}+thread {m['thread_touched']}/{m['tests']} "
                f"setup_fixtures=[{fx}]"
            )
        for f in report["findings"][:_SUMMARY_LINES]:
            tr.write_line(f"  {f['kind']}: {f['nodeid']} {','.join(f['phases'])}")
        extra = len(report["findings"]) - _SUMMARY_LINES
        if extra > 0:
            tr.write_line(f"  ... {extra} more in the report")
        if report["soft_peeks"]:
            tr.write_line(
                f"soft peeks to review: {len(report['soft_peeks'])} sites, "
                f"{c['peek_only']} peek-only tests (never failing)"
            )
        if late_child or late_thread:
            owners = sorted({m for m, v in report["modules"].items() if v["late_touches"]})
            tr.write_line(
                f"late touches after their test finished: child={late_child} thread={late_thread} "
                f"(owning modules marked: {len(owners)}; see late_*_touches in the report)"
            )
        if report["skipped_modules"] or report["collect_skipped"]:
            tr.write_line(
                f"modules skipped wholesale: {len(report['skipped_modules'])} "
                f"(+{len(report['collect_skipped'])} module-level skips) — unexamined by this sweep"
            )
        if report["recovered_files"]:
            tr.write_line(f"recovered {len(report['recovered_files'])} unreported worker file(s)")
        for problem in self._verdict:
            # report mode shows the problem without changing the exit status
            tr.write_line(problem, red=self.mode == "fail", yellow=self.mode != "fail")
        tr.write_line(f"storage-touch report: {path}")


def _split_current_test(value: str) -> tuple[str, str]:
    """``PYTEST_CURRENT_TEST`` is ``<nodeid> (<phase>)``."""
    head, sep, tail = value.rpartition(" (")
    return (head, tail.rstrip(")")) if sep else (value, "")


def _skip_reason(report) -> str | None:
    try:
        if hasattr(report, "wasxfail"):
            return str(report.wasxfail) or "xfail"
        longrepr = report.longrepr
        text = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
        return text.removeprefix("Skipped: ")
    except Exception:  # noqa: BLE001 — a reason is a nicety
        return None


def _relative(entry: str) -> str:
    """A child's ``abs/path.py:12 in fn`` stack entry, cwd-relative when it can be."""
    head, sep, rest = entry.partition(":")
    return f"{_rel(head)}{sep}{rest}"


def _describe(f: Finding) -> str:
    if f.kind == "stale_exemption":
        return (
            f"{f.nodeid}: marked {EXEMPT_MARKER} but constructed no guarded error "
            f"({', '.join(name for _, name in GUARDED)}); "
            "the exemption is stale, remove the marker"
        )
    where = f" from {f.origin}" if f.origin else ""
    fixture = f" (fixture {', '.join(f.fixtures)})" if f.fixtures else ""
    on_thread = " on a thread the test did not run on" if f.kind == "thread_touch" else ""
    return (
        f"{f.nodeid}: reached storage without a tenant "
        f"({', '.join(f.error_types)} constructed{on_thread} in {'/'.join(f.phases)}{fixture}{where}). "
        f"Request the t2_tenant fixture (or mark it {TENANT_MARKER}), or mark "
        f"it {EXEMPT_MARKER} if raising is the point.\n  "
        + "\n  ".join(f.stack)
    )


# ── plugin entry points ──────────────────────────────────────────────────────


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{EXEMPT_MARKER}: the test constructs a guarded storage error (ServiceEndpointUnresolvableError, "
        "T1ServerNotFoundError, ManagedService*, ServiceTokenUnresolvableError) on purpose "
        "(resolver-subject test); exempt from the storage-touch guard, and reported stale if it "
        "never does (nexus-j23ni)",
    )
    config.addinivalue_line(
        "markers",
        f"{TENANT_MARKER}: provision the opt-in test tenant for this test or module "
        "(NX_TEST_T2_SUBSTRATE=optin; a no-op in the default mode) (nexus-j23ni)",
    )
    value = substrate_value()
    if value is not None and value not in KNOWN_VALUES and value not in RETIRED_VALUES:
        raise pytest.UsageError(
            f"{SUBSTRATE_ENV}={value!r} is not a substrate: use 'none' (provision nothing), 'optin' "
            f"(the default: a tenant only for tests that ask for one) or '{PER_TEST_VALUE}' "
            "(a tenant for every test, the escape hatch)"
        )
    if not optin_selected():
        return
    mode = os.environ.get(MODE_ENV, "fail")
    if mode not in ("report", "fail"):
        raise pytest.UsageError(f"{MODE_ENV} must be 'report' or 'fail', got {mode!r}")
    guard = Guard(mode, config)
    config.stash[_GUARD_KEY] = guard
    config.pluginmanager.register(guard, "nx-storage-touch-guard")
    # The run id travels to xdist workers through the environment; a worker reads it.
    if not guard.is_worker:
        guard._set_env(RUN_ID_ENV, guard.run_id)
        if guard.nested:
            # before any xdist worker is spawned: its workers must not inherit the
            # OUTER session's report directory (they fall back to their basetemp,
            # under this controller's)
            guard._set_env(REPORT_DIR_ENV, None)
    _guards.append(guard)
    _install()
    try:
        guard.run_canary()
    except BaseException:
        pytest_unconfigure(config)  # pytest does not unconfigure a session that failed to configure
        raise


def pytest_unconfigure(config: pytest.Config) -> None:
    guard = config.stash.get(_GUARD_KEY, None)
    if guard is None:
        return
    del config.stash[_GUARD_KEY]
    _guards.remove(guard)
    _uninstall()
    guard.restore_env()


_GUARD_KEY = pytest.StashKey[Guard]()


@contextlib.contextmanager
def tenant_active() -> Iterator[None]:
    """Mark the enclosed span as provisioned. The tenant fixtures wrap their
    body in this; with no guard active it does nothing."""
    guard = _guards[-1] if _guards else None
    if guard is None:
        yield
        return
    guard.tenant_depth += 1
    guard.ever_provisioned = True
    try:
        yield
    finally:
        guard.tenant_depth -= 1


@pytest.fixture(autouse=True)
def _reset_vector_lease_cache() -> Iterator[None]:
    """Drop ``http_vector_client._lease_cache`` before and after every test.

    The cache is process-wide and filled on any successful lease discovery, so
    a test's fake lease would otherwise serve a later test (nexus-j23ni).

    Under ``NX_TEST_T2_SUBSTRATE=optin`` also drop the client singleton and its
    probe memos (``reset_http_vector_client_for_tests``): a warm
    ``embedding_mode`` memo would let a later test reach T3 without resolving
    anything, hiding the touch, and a cold failed probe would stay failed for
    the life of the process and poison a later provisioned test.
    """
    from nexus.db import http_vector_client

    def reset() -> None:
        http_vector_client._invalidate_endpoint()
        if optin_selected():
            http_vector_client.reset_http_vector_client_for_tests()

    reset()
    yield
    reset()
