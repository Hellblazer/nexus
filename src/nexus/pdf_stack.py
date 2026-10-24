# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PDF stack on demand (nexus-2k66t.4.2).

docling, MinerU, torch, torchvision and pdftext are the ``pdf`` extra. A fresh
install goes without them (57-65% of the download, 49% of the installed size);
the first PDF index fetches them. An EXISTING install carries them forward on
its first post-split upgrade (``install_layout.receipt_predates_pdf_split``).

THE CONTRACT IS NEVER SILENT. nexus-2fyb is why: an opt-in extra produced
silent formula loss on fresh installs for weeks. So every PDF entry point calls
:func:`ensure` BEFORE it writes a catalog row or a T3 chunk, and the answer is
one of three: the stack is present and the command goes on; it was installed
and the command is re-run in the new generation; or the command stops with an
exit code and the exact command to run. Nothing falls back to a plainer
extractor.

* exit 69 (EX_UNAVAILABLE): absent, and the install was refused (policy
  ``refuse``, a layout the auto path does not touch, a Windows box without the
  VC++ runtime).
* exit 75 (EX_TEMPFAIL): the fetch failed on the network or TLS. Retryable.
* exit 70 (EX_SOFTWARE): the installer failed for another reason, or the stack
  is still not there after a reported success.

HOW THE INSTALL RUNS. Through ``perform_self_install(add_extras=("pdf",),
version=<running>)``: the generation builder with its own arguments (CPU torch
on Linux, the overrides, the constraints shipped in the wheel). A bare
``uv pip install conexus[pdf]`` would resolve CUDA torch on Linux, because
``torch==2.8.0`` in the constraints matches the CPU and CUDA builds alike. The
version pin is load-bearing: without it the fetch is silently an upgrade. The
receipt records the extra, so every later ``nx self install``, the SessionStart
lockstep and ``claude-restart-sessions`` carry it.

The running process cannot import the stack it just installed (it runs from
the old generation), so the command re-runs as a child of
``<tools>/current/bin/nx``; ``NX_PDF_STACK_REEXEC=1`` stops a loop.

State detection never imports torch: ``find_spec`` on the top-level names and
``importlib.metadata`` versions against the pins shipped in
``_install/constraints.txt``.
"""
from __future__ import annotations

import contextlib
import enum
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import structlog

from nexus.util.nx_argv import argv_off_cwd

_log = structlog.get_logger(__name__)

#: Distributions that make up the stack, with the module a ``find_spec`` probe
#: resolves for each. Pinned against ``pyproject.toml``'s ``pdf`` extra by
#: ``tests/test_pdf_stack.py``.
STACK_DISTS: tuple[str, ...] = ("docling-slim", "mineru", "torch", "torchvision", "pdftext")
STACK_MODULES: tuple[str, ...] = ("docling", "mineru", "torch", "torchvision", "pdftext")

POLICY_ENV = "NX_PDF_STACK"
REEXEC_ENV = "NX_PDF_STACK_REEXEC"
POLICY_INSTALL = "install"
POLICY_REFUSE = "refuse"

EXIT_REFUSED = 69
EXIT_INSTALLER = 70
EXIT_NETWORK = 75

#: The command that installs the stack now, on a generation install.
INSTALL_COMMAND = "nx self install --extras pdf"

#: The Visual C++ redistributable, for a Windows box without it.
VCRT_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

#: Said once, on stderr, before uv starts (the design's wording).
INSTALL_NOTICE = (
    "Installing PDF support for conexus {version}: about 240 MB (macOS) / 370 MB (Linux) "
    "download, 0.8 GB on disk, kept by future upgrades. Model weights follow on first "
    "use: about 0.5 GB, plus 1.2 GB for the first PDF with formulas."
)

_SKIPPED_RECORD = "pdf-stack-skipped.json"


def _say(message: str) -> None:
    """One line on stderr. The user-facing channel of a CLI entry point's
    refusal and of the install notice; stdout stays the command's own."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


class PdfStackError(RuntimeError):
    """The stack is missing and the command cannot go on. ``exit_code`` is the
    process status a CLI entry point exits with; the message names the command
    to run."""

    exit_code: int = EXIT_REFUSED


class PdfStackMissingError(PdfStackError):
    """Absent or broken, and the install was refused (69)."""

    exit_code = EXIT_REFUSED


class PdfStackNetworkError(PdfStackError):
    """The fetch could not reach the index (75, retryable)."""

    exit_code = EXIT_NETWORK


class PdfStackInstallError(PdfStackError):
    """The installer failed, or the stack is still absent after it (70)."""

    exit_code = EXIT_INSTALLER


class RerunRequired(BaseException):  # noqa: N818 - a control signal, not an error
    """The stack was installed deep inside a command that holds a lock (``nx
    index repo`` holds the repo's index lock), so the command must unwind
    first and re-run in the new generation. A BaseException so the broad
    ``except Exception`` containment between the gate and the CLI cannot
    swallow it; ``finally`` clauses (the lock release) still run."""


class Outcome(enum.Enum):
    """What :func:`ensure` did when it returned instead of raising."""

    PRESENT = "present"
    #: Installed into a new generation. THIS process cannot import it; the
    #: caller re-runs the command (:func:`ensure_or_exit` does).
    INSTALLED = "installed"


@dataclass(frozen=True)
class StackState:
    """``present`` (every module resolves and every version is on its pin),
    ``absent`` (none of it is here) or ``broken`` (some of it, or a version off
    its pin, or a receipt that lists ``pdf`` over an empty tree)."""

    status: str
    detail: str = ""
    problems: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return self.status == "present"


# ── state ────────────────────────────────────────────────────────────────────


def constraints_path() -> Path:
    """The pins shipped in THIS wheel (``_install/constraints.txt``)."""
    return Path(__file__).resolve().parent / "_install" / "constraints.txt"


def _pins(names: tuple[str, ...]) -> dict[str, str]:
    """``{dist: version}`` for the requested distributions, from the shipped
    constraints, keeping only the line whose environment marker holds HERE.
    A distribution with no applicable pin is absent from the result and its
    version is not checked."""
    from packaging.markers import InvalidMarker, Marker  # noqa: PLC0415 — stdlib-adjacent, deferred

    try:
        text = constraints_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec, _, marker = line.partition(";")
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s]+)", spec.strip())
        if not m:
            continue
        name = re.sub(r"[-_.]+", "-", m.group(1).lower())
        if name not in names:
            continue
        if marker.strip():
            try:
                if not Marker(marker.strip()).evaluate():
                    continue
            except InvalidMarker:
                continue
        out[name] = m.group(2)
    return out


def _find_spec(name: str) -> bool:
    import importlib.util  # noqa: PLC0415 — stdlib, deferred

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _dist_version(name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — stdlib, deferred

    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _public(version: str) -> str:
    from packaging.version import InvalidVersion, Version  # noqa: PLC0415 — deferred

    try:
        return Version(version).public
    except InvalidVersion:
        return version


def _receipt_lists_pdf() -> bool:
    """Whether the generation this process runs from lists ``pdf`` in its
    receipt. False when there is no receipt (a dev checkout)."""
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred
        from nexus.commands.self_cmd import running_generation  # noqa: PLC0415 — deferred

        receipt = install_layout.read_receipt(running_generation())
    except Exception:  # noqa: BLE001 — no readable receipt: no claim
        return False
    return "pdf" in receipt.extras


def state() -> StackState:
    """Classify the stack in THIS interpreter. Never imports torch."""
    found = {mod: _find_spec(mod) for mod in STACK_MODULES}
    missing = [mod for mod, ok in found.items() if not ok]
    if len(missing) == len(STACK_MODULES):
        if _receipt_lists_pdf():
            return StackState(
                "broken", "the receipt lists the pdf extra but none of the stack is installed",
                tuple(missing),
            )
        return StackState("absent", "docling, MinerU and torch are not installed")
    if missing:
        return StackState("broken", f"missing: {', '.join(missing)}", tuple(missing))
    pins = _pins(STACK_DISTS)
    problems: list[str] = []
    for dist in STACK_DISTS:
        installed = _dist_version(dist)
        if installed is None:
            problems.append(f"{dist}: not installed")
            continue
        want = pins.get(dist)
        if want is not None and _public(installed) != _public(want):
            problems.append(f"{dist}: {installed}, the pin is {want}")
    if problems:
        return StackState("broken", "; ".join(problems), tuple(problems))
    return StackState("present", f"{len(STACK_DISTS)} distributions on their pins")


# ── policy ───────────────────────────────────────────────────────────────────


def policy() -> str:
    """``install`` (the default) or ``refuse``. ``NX_PDF_STACK`` wins over the
    ``pdf_stack.auto_install`` config key. An unreadable value refuses: a
    typo must not start a 370 MB download."""
    raw = os.environ.get(POLICY_ENV, "").strip().lower()
    if raw:
        if raw in (POLICY_INSTALL, POLICY_REFUSE):
            return raw
        _log.warning("pdf_stack_policy_unreadable", value=raw, using=POLICY_REFUSE)
        return POLICY_REFUSE
    try:
        from nexus.config import load_config  # noqa: PLC0415 — deferred, heavy

        configured = (load_config().get("pdf_stack") or {}).get("auto_install", True)
    except Exception:  # noqa: BLE001 — config trouble must not decide a download
        return POLICY_INSTALL
    if isinstance(configured, str):
        configured = configured.strip().lower() not in ("false", "0", "no", "off")
    return POLICY_INSTALL if configured else POLICY_REFUSE


# ── the install plan ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InstallPlan:
    kind: str  # generation | directory | legacy | dev
    auto: bool  # whether ensure() may install without a person
    command: str  # what a person runs instead
    why: str = ""


def install_plan() -> InstallPlan:
    """What kind of install this process runs from, and so whether the auto
    path may touch it. The auto path builds a generation; it never rebuilds a
    directory-source install, a uv tool tree or a dev checkout."""
    from nexus import install_layout  # noqa: PLC0415 — deferred
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

    host = self_cmd.running_generation()
    tools = install_layout.tools_dir()
    if self_cmd._same_dir(host.parent, tools) and self_cmd._is_generation_name(host.name):  # noqa: SLF001 — same module family, one rule
        try:
            receipt = install_layout.read_receipt(host)
        except install_layout.InstallLayoutError:
            return InstallPlan(
                "generation", False, "nx self install",
                "this generation has no readable receipt; `nx self install` repairs it",
            )
        if receipt.source_kind == "directory":
            return InstallPlan(
                "directory", False, "NX_REINSTALL_EXTRAS=pdf scripts/reinstall-tool.sh",
                f"this install was built from the checkout {receipt.source}",
            )
        return InstallPlan("generation", True, INSTALL_COMMAND)
    try:
        legacy = self_cmd._running_from_legacy_tool_install()  # noqa: SLF001 — same module family, one rule
    except Exception:  # noqa: BLE001 — unknown layout: do not touch it
        legacy = False
    if legacy:
        return InstallPlan(
            "legacy", False, f"nx self install && {INSTALL_COMMAND}",
            "this is a uv tool install, not a generation; `nx self install` moves it onto the generation layout first",
        )
    return InstallPlan(
        "dev", False, "uv sync",
        "this is a source checkout; its dev group carries the PDF stack",
    )


def _running_version() -> str:
    from importlib.metadata import version  # noqa: PLC0415 — stdlib, deferred

    return version("conexus")


# ── the CURRENT generation (what a peer's install produced) ──────────────────

#: Run by the current generation's own interpreter: whether it resolves every
#: stack module. Never imports one.
_PROBE_SOURCE = (
    "import importlib.util as u, sys\n"
    "sys.exit(0 if all(u.find_spec(m) is not None for m in sys.argv[1:]) else 1)\n"
)
_PROBE_TIMEOUT_S = 30.0


def _current_generation_receipt():
    """``(generation_dir, receipt)`` for what ``<tools>/current`` names, or
    None when there is no readable current generation."""
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred
        from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

        gen = self_cmd._current_generation_dir(install_layout.tools_dir())  # noqa: SLF001 — same module family
        if gen is None:
            return None
        return gen, install_layout.read_receipt(gen)
    except Exception:  # noqa: BLE001 — no readable current generation: no claim
        return None


def current_generation_has_stack() -> bool:
    """Whether the generation ``current`` names already holds the stack: its
    receipt lists ``pdf`` AND its own interpreter resolves every module.

    :func:`state` answers for the RUNNING interpreter, and the running
    interpreter is the old generation: a peer that installed the stack built a
    different generation and flipped ``current`` to it, which ``state()`` cannot
    see. This is the question a waiter asks after it gets the install lock."""
    found = _current_generation_receipt()
    if found is None:
        return False
    gen, receipt = found
    if "pdf" not in receipt.extras:
        return False
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred
        from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

        python = install_layout.venv_python(gen, platform=self_cmd._plat())  # noqa: SLF001
        from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred

        proc = run_bounded(  # the generation's own interpreter, argv list, no shell
            argv_off_cwd([str(python), "-c", _PROBE_SOURCE, *STACK_MODULES]),
            timeout=_PROBE_TIMEOUT_S, text=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ── require / ensure ─────────────────────────────────────────────────────────

_present_cache: bool = False


def reset_cache() -> None:
    """Forget a cached ``present`` verdict. For tests and for a process that
    just installed the stack."""
    global _present_cache
    _present_cache = False


def _remedy(entry: str, st: StackState, plan: InstallPlan | None) -> str:
    plan = plan or install_plan()
    head = "PDF support is not installed" if st.status == "absent" else f"PDF support is broken ({st.detail})"
    why = plan.why[:1].upper() + plan.why[1:] if plan.why else ""
    return (
        f"{head}: {entry} needs docling and MinerU, and nothing was written. "
        f"{why + '. ' if why else ''}Run: {plan.command}"
    )


def missing_message(entry: str = "PDF extraction") -> str | None:
    """The refusal text when the stack is not present, else None. For an error
    that arrives as an ImportError deep in extraction: whether that means "not
    installed" (this text) or "installed and corrupt" (None) is decided here."""
    st = state()
    return None if st.present else _remedy(entry, st, None)


def require(entry: str = "PDF extraction") -> None:
    """Raise :class:`PdfStackMissingError` unless the stack is present. Never
    installs: the library-level guard (``PDFExtractor.extract``) and any caller
    that must not start a download. The positive verdict is cached."""
    global _present_cache
    if _present_cache:
        return
    st = state()
    if st.present:
        _present_cache = True
        return
    raise PdfStackMissingError(_remedy(entry, st, None))


def _windows_runtime_missing() -> bool:
    """On Windows, true when the VC++ runtime is absent everywhere the stack's
    extension modules look. False elsewhere."""
    try:
        from nexus import _vcrt  # noqa: PLC0415 — deferred
        import nexus.config as _config  # noqa: PLC0415 — deferred; module attribute so a patched config dir is seen (nexus-78blw)

        if not _vcrt.is_windows():
            return False
        kind, _where = _vcrt.runtime_source(None, str(_config.nexus_config_dir()), None)
    except Exception:  # noqa: BLE001 — cannot tell: do not refuse on a guess
        return False
    return kind == _vcrt.MISSING


def _classify_failure(message: str) -> PdfStackError:
    """Map an installer failure message to the exit-code classes. Reuses
    ``self_cmd``'s diagnosis so the network and TLS wording stays one rule."""
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

    if self_cmd._NETWORK_FAILURE_RE.search(message) or self_cmd._TLS_FAILURE_RE.search(message):  # noqa: SLF001
        return PdfStackNetworkError(
            "PDF support could not be downloaded (network or TLS failure); nothing was written. "
            f"Retry when the network is back, or run `{INSTALL_COMMAND}` ahead of time.\n{message}"
        )
    tail = "\n".join(message.strip().splitlines()[-12:])
    return PdfStackInstallError(
        f"PDF support could not be installed; nothing was written.\n{tail}"
    )


@contextlib.contextmanager
def _install_lock():
    """Serialize concurrent fetches. The waiter re-checks the state after it
    gets the lock, so two PDF indexes started together fetch once."""
    from nexus import _locking, install_layout  # noqa: PLC0415 — deferred

    tools = install_layout.tools_dir()
    try:
        tools.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(tools / ".pdf-stack.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # No place for a lock file: install without one rather than refuse.
        yield
        return
    try:
        _locking.lock_fd(fd, blocking=True)
        yield
    finally:
        with contextlib.suppress(OSError):
            _locking.unlock_fd(fd)
        os.close(fd)


def _install_generation(*, carrying: bool = False) -> None:
    """Build a generation that adds the ``pdf`` extra and flip to it. The one
    place the on-demand install is spelled.

    The version is the running one, unless ``current`` already holds a NEWER
    one: a stale process (a long-lived hook, an old session) must not flip
    ``current`` backward. Then the build is at current's version and keeps
    current's extras, or, when current was built from a checkout, refuses with
    the command that rebuilds it."""
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

    version = _running_version()
    extras: tuple[str, ...] = ("pdf",)
    found = _current_generation_receipt()
    if found is not None:
        _gen, receipt = found
        if _newer(receipt.version, version):
            if receipt.source_kind == "directory":
                raise PdfStackMissingError(
                    f"PDF support is not installed in the current generation ({receipt.version}, built from "
                    f"{receipt.source}), which is newer than this process ({version}); nothing was written. "
                    "Run: NX_REINSTALL_EXTRAS=pdf scripts/reinstall-tool.sh"
                )
            version = receipt.version
            extras = tuple(sorted({"pdf", *receipt.extras}))
    notice = INSTALL_NOTICE.format(version=version)
    if carrying:
        notice = notice.replace("Installing PDF support", "Carrying PDF support forward", 1)
    _say(notice)
    # perform_self_install reports its reap on stdout; this command's stdout is
    # its own (a caller may parse it), so the install's chatter goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        self_cmd.perform_self_install(add_extras=extras, version=version, progress=True)


def _newer(candidate: str, than: str) -> bool:
    """Whether version *candidate* is strictly newer than *than*. An unreadable
    version is not newer (the running version stays the pin)."""
    from packaging.version import InvalidVersion, Version  # noqa: PLC0415 — deferred

    try:
        return Version(candidate) > Version(than)
    except InvalidVersion:
        return False


def ensure(entry: str) -> Outcome:
    """Make the PDF stack available to *entry* (a command name for messages),
    or raise before anything is written.

    Returns ``Outcome.PRESENT`` when this process can go on, ``Outcome.INSTALLED``
    when a new generation now holds the stack and the caller must re-run in it
    (:func:`ensure_or_exit`). Raises a :class:`PdfStackError` carrying the exit
    code otherwise.
    """
    global _present_cache
    st = state()
    if st.present:
        _present_cache = True
        return Outcome.PRESENT
    if os.environ.get(REEXEC_ENV) == "1":
        # The generation we were re-run in does not have it either: a loop
        # guard that is also the "reported success, still absent" check.
        raise PdfStackInstallError(
            f"PDF support was installed but this generation still reports it {st.status} "
            f"({st.detail}); nothing was written. Run `nx doctor` for the module list, "
            f"then `{INSTALL_COMMAND}` again."
        )
    if policy() == POLICY_REFUSE:
        raise PdfStackMissingError(
            _remedy(entry, st, None) + f"\n(refused by {POLICY_ENV}=refuse or pdf_stack.auto_install: false)"
        )
    plan = install_plan()
    if not plan.auto:
        raise PdfStackMissingError(_remedy(entry, st, plan))
    if _windows_runtime_missing():
        raise PdfStackMissingError(
            f"PDF support needs the Microsoft Visual C++ redistributable, which this machine lacks "
            f"and a cloud-mode install has no engine copy to borrow. Install it from {VCRT_URL} "
            f"and run: {plan.command}"
        )
    with _install_lock():
        # A peer may have installed it while we waited. state() reads THIS
        # interpreter, which is the old generation; the peer built and flipped
        # to another one, so the question goes to `current`. The caller re-runs.
        st = state()
        if st.present:
            _present_cache = True
            return Outcome.PRESENT
        if current_generation_has_stack():
            return Outcome.INSTALLED
        try:
            _install_generation()
        except PdfStackError:
            raise
        except Exception as exc:  # noqa: BLE001 — classified below by message
            raise _classify_failure(getattr(exc, "message", None) or str(exc)) from exc
    return Outcome.INSTALLED


def rerun_command(argv: list[str] | None = None) -> int:
    """Run this command again as a child of the NEW ``current`` generation's
    ``nx`` and return its exit status. ``subprocess.run``, not ``os.execv``, so
    Windows behaves the same."""
    from nexus import install_layout  # noqa: PLC0415 — deferred
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

    argv = list(sys.argv[1:] if argv is None else argv)
    nx = install_layout.venv_script(
        install_layout.current_generation(), "nx", platform=self_cmd._plat(),  # noqa: SLF001
    )
    env = dict(os.environ)
    env[REEXEC_ENV] = "1"
    proc = subprocess.run(argv_off_cwd([str(nx), *argv]), env=env, check=False)  # noqa: S603 — resolved nx, argv list, no shell
    return proc.returncode


#: Appended to a network or installer failure of a command that CAN go on
#: without the stack (``nx index repo``: code and prose do not need it), so an
#: offline box is not locked out of indexing its code by a PDF it happens to hold.
HOLD_HINT = (
    "To index code and prose now and hold the PDFs back, run the same command with "
    f"{POLICY_ENV}=refuse (it exits {EXIT_REFUSED} over the held PDFs); "
    f"{INSTALL_COMMAND} installs PDF support when the network is back."
)


def _fail(exc: PdfStackError, *, hint: str = "") -> NoReturn:
    _say(f"nx: {exc}")
    if hint:
        _say(hint)
    raise SystemExit(exc.exit_code)


def ensure_or_exit(entry: str, *, hold_on_refuse: bool = False, defer_rerun: bool = False) -> bool:
    """The CLI form of :func:`ensure`. Returns True to go on. Exits the process
    with the right status on a failure, or with the child's status after a
    re-run in the new generation. With *hold_on_refuse*, a REFUSAL returns
    False instead of exiting (``nx index repo`` indexes code and prose and holds
    the PDFs back); network and installer failures still exit. With
    *defer_rerun*, an install raises :class:`RerunRequired` for the CLI command
    to catch after it has released its locks, instead of re-running here."""
    try:
        outcome = ensure(entry)
    except PdfStackMissingError as exc:
        if hold_on_refuse:
            _say(f"nx: {exc}")
            return False
        _fail(exc)
    except PdfStackError as exc:
        _fail(exc, hint=HOLD_HINT if hold_on_refuse else "")
    if outcome is Outcome.INSTALLED:
        if defer_rerun:
            raise RerunRequired()
        raise SystemExit(rerun_command())
    return True


# ── PDFs a refusing run left behind (doctor reads it) ────────────────────────


def _skipped_path() -> Path:
    import nexus.config as _config  # noqa: PLC0415 — deferred; module attribute so a patched config dir is seen (nexus-78blw)

    return _config.nexus_config_dir() / _SKIPPED_RECORD


def record_skipped(paths: list[Path], *, repo: Path | None = None) -> None:
    """Write the PDFs a refusing ``nx index repo`` run did not index, so
    ``nx doctor`` can count them. Replaces the previous record for *repo*."""
    path = _skipped_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    key = str(repo) if repo is not None else ""
    if paths:
        data[key] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "paths": sorted(str(p) for p in paths)}
    else:
        data.pop(key, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if data:
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        _log.warning("pdf_stack_skipped_record_unwritable", error=str(exc))


def skipped_count() -> int:
    """How many PDFs refusing runs left un-indexed (0 when none, or unreadable)."""
    try:
        data = json.loads(_skipped_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    return sum(len(v.get("paths", [])) for v in data.values() if isinstance(v, dict))


# ── the upgrade rule: an existing install carries the stack forward ──────────

#: After a failed carry-forward, `nx upgrade --auto` (every session start) waits
#: this long before fetching again; a person running `nx upgrade` retries.
CARRY_FORWARD_AUTO_BACKOFF_S: float = 6 * 3600.0

_CARRY_FAILED_SENTINEL = "pdf-carry-forward-failed"


def carry_forward_pending() -> bool:
    """True when this process runs from a generation whose receipt predates the
    PDF split and does not list ``pdf``: an existing install whose first
    post-split upgrade was built by an installer that did not know the rule
    (the OLD ``nx self install`` builds the NEW package, which is slim)."""
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred
        from nexus.commands.self_cmd import running_generation  # noqa: PLC0415 — deferred

        receipt = install_layout.read_receipt(running_generation())
    except Exception:  # noqa: BLE001 — no readable receipt: nothing to carry
        return False
    if "pdf" in receipt.extras or not install_layout.receipt_predates_pdf_split(receipt):
        return False
    return install_plan().kind == "generation"


def carry_forward(*, auto_mode: bool = False) -> str | None:
    """Build the generation that adds ``pdf`` to an existing install, when
    :func:`carry_forward_pending`. Returns a one-line account, or None when
    there was nothing to do. Never raises: a failure is reported in the line
    and retried by the next run (backed off under ``--auto``)."""
    if not carry_forward_pending():
        return None
    try:
        import nexus.config as _config  # noqa: PLC0415 — deferred; module attribute so a patched config dir is seen (nexus-78blw)

        sentinel = _config.nexus_config_dir() / _CARRY_FAILED_SENTINEL
    except Exception:  # noqa: BLE001 — no config dir: no backoff
        sentinel = None
    if auto_mode and sentinel is not None:
        try:
            age = time.time() - sentinel.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < CARRY_FORWARD_AUTO_BACKOFF_S:
            return f"PDF support carry-forward deferred: the last attempt failed {int(age)}s ago"
    try:
        with _install_lock():
            # A peer's carry (the lockstep upgrade, a SessionStart --auto, a
            # pre-change hook) may have built it while we waited.
            if current_generation_has_stack():
                return "PDF support was already carried forward into the current generation"
            _install_generation(carrying=True)
    except Exception as exc:  # noqa: BLE001 — reported, retried next run
        if sentinel is not None:
            with contextlib.suppress(OSError):
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n", encoding="utf-8")
        _log.warning("pdf_stack_carry_forward_failed", error=str(exc))
        return (
            f"PDF support could not be carried forward ({type(exc).__name__}); this install keeps "
            f"working without it and PDF indexing will say so. Retry: {INSTALL_COMMAND}"
        )
    if sentinel is not None:
        with contextlib.suppress(OSError):
            sentinel.unlink(missing_ok=True)
    return "PDF support carried forward into a new generation (existing installs keep it)"
