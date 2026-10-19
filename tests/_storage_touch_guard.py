# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage-touch guard for the opt-in test tenant (nexus-j23ni).

The suite mints a tenant for every test (``_pin_t2_substrate``). The opt-in
mode (``NX_TEST_T2_SUBSTRATE=optin``) mints one only for tests that ask for it
(the ``t2_tenant`` fixture or marker), and this module is what keeps that safe:
a test that reaches storage WITHOUT a tenant must be named, even when the code
under test catches the error and carries on.

How it sees a touch: with no endpoint resolvable, every storage client raises a
guarded error (``GUARDED`` below: the resolver's not-resolvable type, the
managed-service unreachable / incompatible pair, the service_url-without-token
type, and T1's not-found type). A swallowed raise leaves no trace in the test's
outcome, so the guard patches ``__init__`` of each class at CLASS level (it
survives the import-time bindings in the clients) and records, per
construction, the test id, the phase (setup / call / teardown), the fixture
being set up, the origin frame (outermost frame under ``tests/`` or the item's
file) and a short stack. Constructions from threads other than the test's are
counted and ignored: they belong to nobody in particular.

Per test, at teardown:

* a construction while no tenant was active is a finding, unless the test
  carries ``expects_unresolvable_endpoint`` (resolver-subject tests, which
  raise it on purpose);
* an ``expects_unresolvable_endpoint`` test that passed without constructing
  any guarded error is a STALE exemption, also a finding.

Soft peeks: code in ``src/nexus`` also READS the endpoint without ever
constructing an error (``discover_lease``, ``get_credential`` for
``service_url`` / ``service_token``). Those calls are recorded, when made from
a ``src/nexus`` frame while no tenant is active, as a separate review list.
They never fail a test.

Child processes: under optin the guard puts ``tests/_storage_touch_shim`` on
``PYTHONPATH`` (its ``sitecustomize`` installs the same class patches in every
child Python process and appends each construction to
``NX_STORAGE_TOUCH_CHILD_LOG``, attributed through ``PYTEST_CURRENT_TEST``).
The parent folds the lines into the test that spawned the child.

``NX_STORAGE_TOUCH_GUARD_MODE`` is ``report`` (default: findings go to a report,
the test is untouched) or ``fail`` (the teardown raises, naming the test, and
the session ends non-zero if the sweep was vacuous: no provisioned test, no
clean test, no touch event, or fewer workers reported than started). The guard
is active ONLY under ``NX_TEST_T2_SUBSTRATE=optin``; in every other mode it
installs nothing and records nothing. Two things are unconditional:
``_reset_vector_lease_cache`` (``http_vector_client`` caches a discovered lease
process-wide), and, under optin only, the ``http_vector_client`` singleton
reset (a warm ``embedding_mode`` memo would hide a touch, a cold failed probe
would poison the next provisioned test).

Durability: each process appends ONE JSON line per finished test to
``nx-storage-touch-tests-<run>-<pid>-<id>.jsonl`` under
``NX_STORAGE_TOUCH_REPORT_DIR`` (else its basetemp) and flushes, so a killed
worker loses at most the test it was running. The controller (or a serial run)
builds the report from those files: the ones it was handed by reporting
workers plus any left by workers that never reported (``recovered_files``).

Report (``nx-storage-touch-report.json``, path printed in the terminal
summary): ``counts``, ``findings`` (with origin, fixtures, deduped touches),
``modules`` (per file: tests / touched / clean / provisioned / exempt / skipped
/ failed / ``setup_fixtures``), ``soft_peeks``, ``skipped_modules`` (every test
skipped), ``collect_skipped`` (module-level skips), ``late_child_touches``.

A pytest plugin, registered from ``tests/conftest.py``'s ``pytest_plugins``,
for the reason ``tests/_env_restore.py`` gives: a ``pytester`` session can load
it on its own, which is how ``tests/test_storage_touch_guard*.py`` prove it.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

OPTIN_VALUE = "optin"
MODE_ENV = "NX_STORAGE_TOUCH_GUARD_MODE"
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

_WORKEROUTPUT_KEY = "nx_storage_touch_report"
_JSONL_GLOB = "nx-storage-touch-tests-{run}-*.jsonl"
_STACK_FRAMES = 6
_SUMMARY_LINES = 25
_MAX_SIGNATURES = 8  # per test: distinct (phase, fixture, stack) entries kept
_LATE_CHILD_CAP = 50


def optin_selected() -> bool:
    """True when this session runs the opt-in tenant mode."""
    return os.environ.get("NX_TEST_T2_SUBSTRATE") == OPTIN_VALUE


class StorageTouchError(AssertionError):
    """A test reached storage without a tenant (or kept a stale exemption)."""


@dataclass
class Finding:
    nodeid: str
    kind: str  # "unprovisioned_touch" | "stale_exemption"
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
    clean_unprovisioned: int = 0
    stale_exemptions: int = 0
    touch_events: int = 0
    skipped: int = 0
    failed: int = 0
    foreign_thread_events: int = 0


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
    shown: str | Path = path
    with contextlib.suppress(ValueError):
        shown = Path(path).relative_to(Path.cwd())
    return f"{shown}:{lineno} in {name}"


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


def _record_touch(error_type: str) -> None:
    guard = _guards[-1] if _guards else None
    if guard is not None:
        guard.record(error_type)


def _make_patched(cls: type, original):
    def __init__(self, *args, **kwargs):  # noqa: N807 — replacing __init__ by name
        original(self, *args, **kwargs)
        _record_touch(type(self).__name__)

    __init__.__qualname__ = f"{cls.__qualname__}.__init__"
    __init__._nx_storage_touch_patched = True  # type: ignore[attr-defined]
    return __init__


def _record_peek(kind: str, depth: int) -> None:
    guard = _guards[-1] if _guards else None
    if guard is None:
        return
    frame = sys._getframe(depth)
    code = frame.f_code
    if _real(code.co_filename).startswith(_nexus_prefix()):
        guard.record_peek(kind, _fmt((code.co_filename, frame.f_lineno, code.co_name)))


def _make_peek_wrappers() -> dict[str, tuple[object, object]]:
    from nexus import config
    from nexus.db import service_endpoint

    original_credential = config.get_credential
    original_lease = service_endpoint.discover_lease

    @functools.wraps(original_credential)
    def get_credential(*args, **kwargs):
        name = args[0] if args else kwargs.get("name")
        if name in _PEEK_CREDENTIALS:
            _record_peek(f"get_credential:{name}", 2)
        return original_credential(*args, **kwargs)

    @functools.wraps(original_lease)
    def discover_lease(*args, **kwargs):
        _record_peek("discover_lease", 2)
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


# ── records and roll-up ──────────────────────────────────────────────────────


def _unprovisioned(rec: dict) -> list[dict]:
    return [t for t in rec["touches"] if not t["provisioned"]]


def _finding_of(rec: dict) -> Finding | None:
    """The finding a test record amounts to, or None. One definition for the
    in-process teardown check and the merged report."""
    if rec.get("stale"):
        return Finding(nodeid=rec["nodeid"], kind="stale_exemption")
    bad = _unprovisioned(rec)
    if rec["unprov_events"] and not rec["exempt"]:
        return Finding(
            nodeid=rec["nodeid"],
            kind="unprovisioned_touch",
            phases=sorted({t["phase"] for t in bad}),
            error_types=sorted({t["error_type"] for t in bad}),
            fixtures=sorted({t["fixture"] for t in bad if t["fixture"]}),
            origin=next((t["origin"] for t in bad if t["origin"]), None),
            count=rec["unprov_events"],
            dropped_events=rec["dropped_events"],
            stack=bad[0]["stack"] if bad else [],
            touches=bad,
        )
    return None


def _rollup(records: list[dict]) -> dict:
    counts = Counts()
    modules: dict[str, dict] = {}
    findings: list[Finding] = []
    peeks: dict[tuple[str, str], dict] = {}
    collect_skipped: set[str] = set()
    late: list[dict] = []
    for rec in records:
        kind = rec["k"]
        if kind == "collect_skip":
            collect_skipped.add(rec["nodeid"])
            continue
        if kind == "late_child":
            late.append(rec)
            continue
        module = rec["nodeid"].split("::")[0]
        m = modules.setdefault(module, {
            "tests": 0, "touched": 0, "clean": 0, "provisioned": 0, "exempt": 0,
            "skipped": 0, "failed": 0, "setup_fixtures": {},
        })
        finding = _finding_of(rec)
        skipped = rec["outcome"] in ("skipped", "xfailed")
        failed = rec["outcome"] == "failed"
        touched = finding is not None and finding.kind == "unprovisioned_touch"
        clean = (
            not rec["provisioned"] and not rec["events"] and not rec["exempt"]
            and not skipped and not rec.get("stale")
        )
        counts.tests += 1
        counts.touch_events += rec["events"]
        counts.foreign_thread_events += rec.get("foreign_thread_events", 0)
        counts.provisioned += rec["provisioned"]
        counts.exempt += rec["exempt"]
        counts.skipped += skipped
        counts.failed += failed
        counts.touched_unprovisioned += touched
        counts.stale_exemptions += bool(finding and finding.kind == "stale_exemption")
        counts.clean_unprovisioned += clean
        m["tests"] += 1
        m["provisioned"] += rec["provisioned"]
        m["exempt"] += rec["exempt"]
        m["skipped"] += skipped
        m["failed"] += failed
        m["touched"] += touched
        m["clean"] += clean
        if touched:
            for fixture in {t["fixture"] for t in _unprovisioned(rec) if t["phase"] == "setup"}:
                key = fixture or "<hook>"
                m["setup_fixtures"][key] = m["setup_fixtures"].get(key, 0) + 1
        if finding is not None:
            findings.append(finding)
        for p in rec.get("soft_peeks", ()):
            entry = peeks.setdefault((p["kind"], p["site"]), {
                "kind": p["kind"], "site": p["site"], "count": 0, "tests": 0, "example": rec["nodeid"],
            })
            entry["count"] += p["count"]
            entry["tests"] += 1
    findings.sort(key=lambda f: (f.kind, f.nodeid))
    return {
        "counts": asdict(counts),
        "findings": [asdict(f) for f in findings],
        "modules": dict(sorted(modules.items())),
        "soft_peeks": sorted(peeks.values(), key=lambda p: (-p["tests"], p["kind"], p["site"])),
        "skipped_modules": sorted(k for k, v in modules.items() if v["tests"] and v["skipped"] == v["tests"]),
        "collect_skipped": sorted(collect_skipped),
        "late_child_touches": late[:_LATE_CHILD_CAP],
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
        self.guard_id = uuid.uuid4().hex[:8]
        self.run_id = (os.environ.get(RUN_ID_ENV) if self.is_worker else None) or uuid.uuid4().hex[:8]
        self._saved_env: dict[str, str | None] = {}
        self._dir: Path | None = None
        self._jsonl: Path | None = None
        self._jsonl_fh = None
        self._child_log: Path | None = None
        self._child_offset = 0
        self.fixture_stack: list[str] = []
        self.collect_skips: set[str] = set()
        self.workers_started = 0
        self.reported: list[dict] = []
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
        self.events = 0
        self.unprov_events = 0
        self.foreign = 0
        self.sigs: dict[tuple, dict] = {}
        self.dropped_events = 0
        self.peeks: dict[tuple[str, str, str], int] = {}
        self.fixture_stack.clear()

    def _item_path(self) -> str | None:
        path = getattr(self.item, "path", None)
        return _real(str(path)) if path is not None else None

    # recording ------------------------------------------------------------
    def _live(self) -> bool:
        """Attributable to the running test: an item, on that test's thread."""
        return self.item is not None and threading.get_ident() == self.thread

    def record(self, error_type: str) -> None:
        if self.item is None:
            return  # collection / session scope: not attributable to a test
        if threading.get_ident() != self.thread:
            self.foreign += 1
            return
        provisioned = self.tenant_depth > 0
        self.events += 1
        self.unprov_events += not provisioned
        frames = _frames()
        fixture = self.fixture_stack[-1] if self.phase == "setup" and self.fixture_stack else None
        stack = _short_stack(frames)
        key = (self.phase, error_type, fixture, provisioned, "parent", tuple(stack))
        entry = self.sigs.get(key)
        if entry is None:
            if len(self.sigs) >= _MAX_SIGNATURES:
                self.dropped_events += 1
                return
            self.sigs[key] = {
                "phase": self.phase, "error_type": error_type, "fixture": fixture,
                "provisioned": provisioned, "source": "parent", "count": 0,
                "origin": _origin(frames, self._item_path()), "stack": stack,
            }
            entry = self.sigs[key]
        entry["count"] += 1

    def record_peek(self, kind: str, site: str) -> None:
        if not self._live() or self.tenant_depth > 0:
            return
        key = (kind, site, self.phase)
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

    # lifecycle ------------------------------------------------------------
    def _ensure_paths(self) -> None:
        if self._dir is not None:
            return
        configured = os.environ.get(REPORT_DIR_ENV)
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
        self._ensure_paths()
        if self._jsonl_fh is None:
            self._jsonl_fh = self._jsonl.open("a", encoding="utf-8")  # type: ignore[union-attr]
        self._jsonl_fh.write(json.dumps(rec) + "\n")
        self._jsonl_fh.flush()

    def _begin(self, item: pytest.Item) -> None:
        self._ensure_paths()
        self._begin_state()
        self.item = item
        self.phase = "setup"
        self.thread = threading.get_ident()

    def _child_touches(self, item: pytest.Item) -> list[dict]:
        touches: list[dict] = []
        for line in self._drain_children():
            nodeid, _, phase = (line.get("current_test") or "").rpartition(" (")
            stack = [_relative(s) for s in line.get("stack", ())]
            if nodeid != item.nodeid:
                self._write({
                    "k": "late_child", "nodeid": nodeid or "<no test running>",
                    "phase": phase.rstrip(")"), "error_type": line.get("error_type"),
                    "pid": line.get("pid"), "stack": stack,
                })
                continue
            touches.append({
                "phase": phase.rstrip(")"), "error_type": line.get("error_type"), "fixture": None,
                "provisioned": self.ever_provisioned, "source": "child", "count": 1,
                "origin": None, "stack": stack,
            })
        return touches

    def _finalize(self, item: pytest.Item, *, raise_on_finding: bool) -> None:
        # the literal name is what tests/test_marker_selection_coverage.py looks for
        exempt = item.get_closest_marker("expects_unresolvable_endpoint") is not None
        touches = list(self.sigs.values())
        child = self._child_touches(item)
        touches.extend(child)
        events = self.events + len(child)
        unprov = self.unprov_events + sum(not t["provisioned"] for t in child)
        rec = {
            "k": "test", "nodeid": item.nodeid, "outcome": self.outcome,
            "provisioned": self.ever_provisioned, "exempt": exempt,
            "events": events, "unprov_events": unprov,
            "foreign_thread_events": self.foreign,
            "dropped_events": self.dropped_events,
            "touches": touches,
            "soft_peeks": [
                {"kind": k, "site": s, "phase": ph, "count": n} for (k, s, ph), n in sorted(self.peeks.items())
            ],
            "stale": bool(exempt and not events and self.call_passed),
        }
        self._write(rec)
        finding = _finding_of(rec)
        self._begin_state()
        if finding is not None and raise_on_finding and self.mode == "fail":
            raise StorageTouchError(_describe(finding))

    # hooks ------------------------------------------------------------------
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        self._begin(item)
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.fixture_stack.append(fixturedef.argname)
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
        })
        self._merged = report
        return report

    def _vacuity(self, report: dict) -> list[str]:
        c = report["counts"]
        problems = [
            f"vacuous sweep: {name}=0 (must be > 0)"
            for name in ("provisioned", "clean_unprovisioned", "touch_events")
            if not c[name]
        ]
        if report["workers"] != report["workers_started"]:
            problems.append(
                f"vacuous sweep: workers reported {report['workers']} of {report['workers_started']} started"
            )
        return problems

    def pytest_sessionfinish(self, session):
        config = session.config
        workeroutput = getattr(config, "workeroutput", None)
        if workeroutput is not None:
            self._ensure_paths()
            for rec in self._drain_children():  # stragglers the last test did not see
                self._write({
                    "k": "late_child", "nodeid": rec.get("current_test") or "<no test running>",
                    "error_type": rec.get("error_type"), "pid": rec.get("pid"),
                    "stack": [_relative(s) for s in rec.get("stack", ())],
                })
            if self._jsonl_fh is not None:
                self._jsonl_fh.flush()
            workeroutput[_WORKEROUTPUT_KEY] = {"jsonl": str(self._jsonl), "pid": os.getpid()}
            return
        if self.mode == "fail" and not config.option.collectonly:
            self._verdict = self._vacuity(self.merged())
            if self._verdict and not session.exitstatus:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if hasattr(config, "workerinput"):
            return
        report = self.merged()
        path = (self._dir or _basetemp(config)) / REPORT_NAME
        path.write_text(json.dumps(report, indent=1))
        c = report["counts"]
        tr = terminalreporter
        tr.section("storage-touch guard (nexus-j23ni)")
        tr.write_line(
            f"mode={report['mode']} tests={c['tests']} provisioned={c['provisioned']} "
            f"touched_unprovisioned={c['touched_unprovisioned']} "
            f"clean_unprovisioned={c['clean_unprovisioned']} exempt={c['exempt']} "
            f"stale_exemptions={c['stale_exemptions']} touch_events={c['touch_events']} "
            f"skipped={c['skipped']} failed={c['failed']}"
        )
        worst = sorted(
            ((name, m) for name, m in report["modules"].items() if m["touched"]),
            key=lambda kv: -kv[1]["touched"],
        )
        for name, m in worst[:_SUMMARY_LINES]:
            fx = ",".join(f"{k}x{v}" for k, v in sorted(m["setup_fixtures"].items()))
            tr.write_line(f"  module {name}: touched={m['touched']}/{m['tests']} setup_fixtures=[{fx}]")
        for f in report["findings"][:_SUMMARY_LINES]:
            tr.write_line(f"  {f['kind']}: {f['nodeid']} {','.join(f['phases'])}")
        extra = len(report["findings"]) - _SUMMARY_LINES
        if extra > 0:
            tr.write_line(f"  ... {extra} more in the report")
        if report["soft_peeks"]:
            tr.write_line(f"soft peeks to review: {len(report['soft_peeks'])} sites (never failing)")
        if report["skipped_modules"] or report["collect_skipped"]:
            tr.write_line(
                f"modules skipped wholesale: {len(report['skipped_modules'])} "
                f"(+{len(report['collect_skipped'])} module-level skips) — unexamined by this sweep"
            )
        if report["recovered_files"]:
            tr.write_line(f"recovered {len(report['recovered_files'])} unreported worker file(s)")
        for problem in self._verdict:
            tr.write_line(problem, red=True)
        tr.write_line(f"storage-touch report: {path}")


def _relative(entry: str) -> str:
    """A child's ``abs/path.py:12 in fn`` stack entry, cwd-relative when it can be."""
    head, sep, rest = entry.partition(":")
    shown: str | Path = head
    with contextlib.suppress(ValueError):
        shown = Path(head).relative_to(Path.cwd())
    return f"{shown}{sep}{rest}"


def _describe(f: Finding) -> str:
    if f.kind == "stale_exemption":
        return (
            f"{f.nodeid}: marked {EXEMPT_MARKER} but constructed no guarded error "
            f"({', '.join(name for _, name in GUARDED)}); "
            "the exemption is stale, remove the marker"
        )
    where = f" from {f.origin}" if f.origin else ""
    fixture = f" (fixture {', '.join(f.fixtures)})" if f.fixtures else ""
    return (
        f"{f.nodeid}: reached storage without a tenant "
        f"({', '.join(f.error_types)} constructed in {'/'.join(f.phases)}{fixture}{where}). "
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
    if not optin_selected():
        return
    mode = os.environ.get(MODE_ENV, "report")
    if mode not in ("report", "fail"):
        raise pytest.UsageError(f"{MODE_ENV} must be 'report' or 'fail', got {mode!r}")
    guard = Guard(mode, config)
    config.stash[_GUARD_KEY] = guard
    config.pluginmanager.register(guard, "nx-storage-touch-guard")
    # The run id travels to xdist workers through the environment; a worker reads it.
    if not guard.is_worker:
        guard._set_env(RUN_ID_ENV, guard.run_id)
    _guards.append(guard)
    _install()


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
