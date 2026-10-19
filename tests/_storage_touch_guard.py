# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage-touch guard for the opt-in test tenant (nexus-j23ni).

The suite mints a tenant for every test (``_pin_t2_substrate``). The opt-in
mode (``NX_TEST_T2_SUBSTRATE=optin``) mints one only for tests that ask for it
(the ``t2_tenant`` fixture or marker), and this module is what keeps that safe:
a test that reaches storage WITHOUT a tenant must be named, even when the code
under test catches the error and carries on.

How it sees a touch: with no endpoint resolvable, every storage client raises
``ServiceEndpointUnresolvableError`` (T1/T2/catalog/T3) or
``T1ServerNotFoundError`` (T1). A swallowed raise leaves no trace in the test's
outcome, so the guard patches ``__init__`` of both classes at CLASS level (it
survives the import-time bindings in the clients) and records the test id, the
phase (setup / call / teardown) and a short stack for every construction.

Per test, at teardown:

* a construction while no tenant was active is a finding, unless the test
  carries ``expects_unresolvable_endpoint`` (resolver-subject tests, which
  raise it on purpose);
* an ``expects_unresolvable_endpoint`` test that passed without constructing
  either error is a STALE exemption, also a finding.

``NX_STORAGE_TOUCH_GUARD_MODE`` is ``report`` (default: findings go to a JSON
report, the test is untouched) or ``fail`` (the teardown raises, naming the
test). The guard is active ONLY under ``NX_TEST_T2_SUBSTRATE=optin``; in every
other mode this module installs nothing and records nothing. The one
unconditional behaviour is ``_reset_vector_lease_cache``: ``http_vector_client``
caches a discovered lease process-wide, so one test's fake lease could serve
the next.

Report: each xdist worker writes ``nx-storage-touch-worker.json`` under its
basetemp and hands the path to the controller through ``workeroutput``; the
controller (or a serial run) merges them into ``nx-storage-touch-report.json``
under its own basetemp and prints that path in the terminal summary.

A pytest plugin, registered from ``tests/conftest.py``'s ``pytest_plugins``,
for the reason ``tests/_env_restore.py`` gives: a ``pytester`` session can load
it on its own, which is how ``tests/test_storage_touch_guard.py`` proves it.

Known limit: a construction inside a child process (``nx`` subprocesses, hook
verbs) is invisible here.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import traceback
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

OPTIN_VALUE = "optin"
MODE_ENV = "NX_STORAGE_TOUCH_GUARD_MODE"
EXEMPT_MARKER = "expects_unresolvable_endpoint"
TENANT_MARKER = "t2_tenant"
WORKER_REPORT_NAME = "nx-storage-touch-worker.json"
REPORT_NAME = "nx-storage-touch-report.json"
_WORKEROUTPUT_KEY = "nx_storage_touch_report"
_STACK_FRAMES = 6
_SUMMARY_LINES = 25


def optin_selected() -> bool:
    """True when this session runs the opt-in tenant mode."""
    return os.environ.get("NX_TEST_T2_SUBSTRATE") == OPTIN_VALUE


class StorageTouchError(AssertionError):
    """A test reached storage without a tenant (or kept a stale exemption)."""


@dataclass
class Touch:
    phase: str
    error_type: str
    stack: list[str]
    provisioned: bool


@dataclass
class Finding:
    nodeid: str
    kind: str  # "unprovisioned_touch" | "stale_exemption"
    phases: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)


@dataclass
class Counts:
    tests: int = 0
    provisioned: int = 0
    exempt: int = 0
    touched_unprovisioned: int = 0
    clean_unprovisioned: int = 0
    stale_exemptions: int = 0
    touch_events: int = 0

    def add(self, other: Counts) -> None:
        for name in asdict(self):
            setattr(self, name, getattr(self, name) + getattr(other, name))


# ── class-level patch ────────────────────────────────────────────────────────

_guards: list[Guard] = []  # innermost last; a nested pytester session pushes
_patch_lock = threading.Lock()
_patch_refs = 0
_originals: dict[type, tuple[bool, object]] = {}


def _guarded_classes() -> tuple[type, ...]:
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError
    from nexus.db.t1 import T1ServerNotFoundError

    return (ServiceEndpointUnresolvableError, T1ServerNotFoundError)


def _short_stack() -> list[str]:
    here = Path(__file__).resolve()
    frames = [
        f for f in traceback.extract_stack()
        if Path(f.filename).resolve() != here
    ]
    cwd = Path.cwd()
    out: list[str] = []
    for f in frames[-_STACK_FRAMES:]:
        path = Path(f.filename)
        with contextlib.suppress(ValueError):
            path = path.relative_to(cwd)
        out.append(f"{path}:{f.lineno} in {f.name}")
    return out


def _record_touch(error_type: str) -> None:
    guard = _guards[-1] if _guards else None
    if guard is not None:
        guard.record(error_type)


def _make_patched(cls: type, original):
    def __init__(self, *args, **kwargs):  # noqa: N807 — replacing __init__ by name
        original(self, *args, **kwargs)
        _record_touch(type(self).__name__)

    __init__.__qualname__ = f"{cls.__qualname__}.__init__"
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


# ── per-session state ────────────────────────────────────────────────────────


class Guard:
    """One session's recorder. Registered as a plugin so its hooks run."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.item: pytest.Item | None = None
        self.phase = ""
        self.tenant_depth = 0
        self.ever_provisioned = False
        self.call_passed = False
        self.touches: list[Touch] = []
        self.counts = Counts()
        self.findings: list[Finding] = []
        self.worker_reports: list[dict] = []
        self._lock = threading.Lock()

    # recording ------------------------------------------------------------
    def record(self, error_type: str) -> None:
        if self.item is None:
            return  # collection / session scope: not attributable to a test
        touch = Touch(
            phase=self.phase,
            error_type=error_type,
            stack=_short_stack(),
            provisioned=self.tenant_depth > 0,
        )
        with self._lock:
            self.touches.append(touch)

    def _begin(self, item: pytest.Item) -> None:
        self.item = item
        self.phase = "setup"
        self.tenant_depth = 0
        self.ever_provisioned = False
        self.call_passed = False
        self.touches = []

    def _finalize(self, item: pytest.Item, *, raise_on_finding: bool) -> None:
        touches, self.touches = self.touches, []
        exempt = item.get_closest_marker("expects_unresolvable_endpoint") is not None
        c = self.counts
        c.tests += 1
        c.touch_events += len(touches)
        c.provisioned += self.ever_provisioned
        c.exempt += exempt
        bad = [t for t in touches if not t.provisioned]
        new: list[Finding] = []
        if bad and not exempt:
            c.touched_unprovisioned += 1
            new.append(Finding(
                nodeid=item.nodeid,
                kind="unprovisioned_touch",
                phases=sorted({t.phase for t in bad}),
                error_types=sorted({t.error_type for t in bad}),
                stack=bad[0].stack,
            ))
        elif exempt and not touches and self.call_passed:
            c.stale_exemptions += 1
            new.append(Finding(nodeid=item.nodeid, kind="stale_exemption"))
        elif not self.ever_provisioned and not touches and not exempt:
            c.clean_unprovisioned += 1
        self.findings.extend(new)
        self.item = None
        self.phase = ""
        if new and raise_on_finding and self.mode == "fail":
            raise StorageTouchError(_describe(new[0]))

    # hooks ------------------------------------------------------------------
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        self._begin(item)
        return (yield)

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

    # report -----------------------------------------------------------------
    def _payload(self) -> dict:
        return {
            "mode": self.mode,
            "counts": asdict(self.counts),
            "findings": [asdict(f) for f in self.findings],
        }

    def pytest_sessionfinish(self, session):
        config = session.config
        workeroutput = getattr(config, "workeroutput", None)
        if workeroutput is None:
            return
        path = _basetemp(config) / WORKER_REPORT_NAME
        path.write_text(json.dumps(self._payload()))
        workeroutput[_WORKEROUTPUT_KEY] = str(path)

    @pytest.hookimpl(optionalhook=True)  # xdist's hook; absent under -p no:xdist
    def pytest_testnodedown(self, node, error):
        path = (getattr(node, "workeroutput", None) or {}).get(_WORKEROUTPUT_KEY)
        if path and Path(path).is_file():
            self.worker_reports.append(json.loads(Path(path).read_text()))

    def merged(self) -> dict:
        counts = Counts()
        findings: list[dict] = []
        for part in (self._payload(), *self.worker_reports):
            counts.add(Counts(**part["counts"]))
            findings.extend(part["findings"])
        findings.sort(key=lambda f: (f["kind"], f["nodeid"]))
        return {
            "mode": self.mode,
            "workers": len(self.worker_reports),
            "counts": asdict(counts),
            "findings": findings,
        }

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if hasattr(config, "workerinput"):
            return
        report = self.merged()
        path = _basetemp(config) / REPORT_NAME
        path.write_text(json.dumps(report, indent=1))
        c = report["counts"]
        tr = terminalreporter
        tr.section("storage-touch guard (nexus-j23ni)")
        tr.write_line(
            f"mode={report['mode']} tests={c['tests']} provisioned={c['provisioned']} "
            f"touched_unprovisioned={c['touched_unprovisioned']} "
            f"clean_unprovisioned={c['clean_unprovisioned']} exempt={c['exempt']} "
            f"stale_exemptions={c['stale_exemptions']} touch_events={c['touch_events']}"
        )
        for f in report["findings"][:_SUMMARY_LINES]:
            tr.write_line(f"  {f['kind']}: {f['nodeid']} {','.join(f['phases'])}")
        extra = len(report["findings"]) - _SUMMARY_LINES
        if extra > 0:
            tr.write_line(f"  ... {extra} more in the report")
        tr.write_line(f"storage-touch report: {path}")


def _describe(f: Finding) -> str:
    if f.kind == "stale_exemption":
        return (
            f"{f.nodeid}: marked {EXEMPT_MARKER} but constructed neither "
            "ServiceEndpointUnresolvableError nor T1ServerNotFoundError; "
            "the exemption is stale, remove the marker"
        )
    return (
        f"{f.nodeid}: reached storage without a tenant "
        f"({', '.join(f.error_types)} constructed in {'/'.join(f.phases)}). "
        f"Request the t2_tenant fixture (or mark it {TENANT_MARKER}), or mark "
        f"it {EXEMPT_MARKER} if raising is the point.\n  "
        + "\n  ".join(f.stack)
    )


def _basetemp(config: pytest.Config) -> Path:
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        return factory.getbasetemp()
    import tempfile

    return Path(tempfile.mkdtemp(prefix="nx-storage-touch-"))


# ── plugin entry points ──────────────────────────────────────────────────────


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{EXEMPT_MARKER}: the test constructs ServiceEndpointUnresolvableError / "
        "T1ServerNotFoundError on purpose (resolver-subject test); exempt from the "
        "storage-touch guard, and reported stale if it never does (nexus-j23ni)",
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
    guard = Guard(mode)
    config.stash[_GUARD_KEY] = guard
    config.pluginmanager.register(guard, "nx-storage-touch-guard")
    _guards.append(guard)
    _install()


def pytest_unconfigure(config: pytest.Config) -> None:
    guard = config.stash.get(_GUARD_KEY, None)
    if guard is None:
        return
    del config.stash[_GUARD_KEY]
    _guards.remove(guard)
    _uninstall()


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
    """
    from nexus.db import http_vector_client

    http_vector_client._invalidate_endpoint()
    yield
    http_vector_client._invalidate_endpoint()
