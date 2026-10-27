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
``torch==<version>`` in the constraints matches the CPU and CUDA builds alike. The
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
from collections.abc import Sequence
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

#: The one extractor that reads a PDF with PyMuPDF alone (a core dependency): text layer only,
#: no layout, tables or math. It needs none of the stack (RDR-237).
TEXT_ONLY_EXTRACTOR = "pymupdf"

#: How an entry point lets the user choose the extractor, which decides how a refusal names the
#: text-only way out (:func:`text_only_remedy`). A user can only be sent to a flag or a setting the
#: command reads.
CHOICE_FLAG_AND_SETTING = "flag and setting"
CHOICE_SETTING = "setting"
CHOICE_FLAG = "flag"
CHOICE_NONE = "none"

#: The ``entry`` string each CLI gate passes -> how that command chooses its extractor. The census
#: (``tests/test_pdf_stack_entry_point_census.py``) fails when a gate names an entry missing here.
#: An entry not listed (the library backstops: ``index_pdf``, ``PDFExtractor.extract``) is a
#: library call and is told to pass the extractor by name.
ENTRY_EXTRACTOR_CHOICE: dict[str, str] = {
    "nx index pdf": CHOICE_FLAG_AND_SETTING,
    "nx index pdf --dir": CHOICE_FLAG_AND_SETTING,
    "nx index repo": CHOICE_SETTING,
    "nx collection reindex": CHOICE_SETTING,
    # `nx dt index` and `nx dt capture` read the flag only (default `auto`, never `pdf.extractor`);
    # if they come to read the setting, change these two to CHOICE_FLAG_AND_SETTING.
    "nx dt index": CHOICE_FLAG,
    "nx dt capture": CHOICE_FLAG,
    "nx mineru start": CHOICE_NONE,
}

_TEXT_ONLY = "For text only (no layout, tables or math), "
_NEEDS_NO_STACK = ": that needs no PDF support."


def text_only_remedy(entry: str) -> str:
    """The sentence a refusal appends to name the text-only way out for *entry*, in the words of
    how that command chooses its extractor; empty for a command that has no extractor
    (``nx mineru start`` starts the MinerU server)."""
    choice = ENTRY_EXTRACTOR_CHOICE.get(entry)
    if choice == CHOICE_FLAG_AND_SETTING:
        return f"{_TEXT_ONLY}rerun with `--extractor pymupdf` (or set `pdf.extractor: pymupdf`){_NEEDS_NO_STACK}"
    if choice == CHOICE_SETTING:
        return f"{_TEXT_ONLY}set `pdf.extractor: pymupdf` and rerun{_NEEDS_NO_STACK}"
    if choice == CHOICE_FLAG:
        return f"{_TEXT_ONLY}rerun with `--extractor pymupdf`{_NEEDS_NO_STACK}"
    if choice == CHOICE_NONE:
        return ""
    return f"{_TEXT_ONLY}extract with the `pymupdf` extractor (`extractor=\"pymupdf\"`){_NEEDS_NO_STACK}"


def _with_text_only_remedy(exc: PdfStackError, entry: str) -> PdfStackError:
    """*exc* with the text-only sentence for *entry* appended, once. Every refusal and failure of
    :func:`ensure` and :func:`require` passes through here, so the Windows runtime gap, the
    newer-generation refusal, the network and installer failures and the re-exec guard name the
    way out as the missing-stack message does, in one place."""
    sentence = text_only_remedy(entry)
    message = str(exc)
    if sentence and sentence not in message:
        exc.args = (f"{message}\n{sentence}",)
    return exc


def needs_stack(extractor: str = "") -> bool:
    """Whether *extractor* needs the PDF stack. False only for the explicit ``pymupdf``
    extractor; an empty or unknown name needs it, so a gate that was not given the extractor
    stays as strict as it always was."""
    return extractor != TEXT_ONLY_EXTRACTOR

#: The command that installs the stack now, on a GENERATION install. Every other
#: layout refuses ``--extras`` ("this nx is not running from one"), so a message a
#: non-generation install can reach names :func:`repair_command`, never this.
INSTALL_COMMAND = "nx self install --extras pdf"

#: The Visual C++ redistributable, for a Windows box without it.
VCRT_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

#: Said once, on stderr, before uv starts (the design's wording).
INSTALL_NOTICE = (
    "Installing PDF support for conexus {version}: about 285 MB (macOS) / 390 MB (Linux) "
    "download, 1.0 GB (macOS) / 1.3 GB (Linux) on disk, kept by future upgrades. Model weights follow on first "
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
    typo must not start a 390 MB download."""
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
    kind: str  # generation | directory | legacy | mcpb | dev
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
        # ONE command (nexus-2k66t.4.9): `nx self install` converges a legacy tree onto the
        # generation layout and, because the tree is an existing install, carries `pdf` with it
        # (migrate_legacy.sh, generation_core.migrate_legacy). A second `--extras pdf` was redundant.
        return InstallPlan(
            "legacy", False, "nx self install",
            "this is a uv tool install, not a generation; `nx self install` moves it onto the generation "
            "layout and carries PDF support with it",
        )
    if _running_from_mcpb_bundle(host):
        if os.environ.get(MCPB_PDF_SETTING_ENV, "").strip().lower() in ("1", "true"):
            # nexus-dzc61: the launcher passes the setting through to the server, so a stack that is
            # missing or broken with it on is a fallback or a mismatch, never a setting to turn on
            why = ("this is the Claude Desktop extension (.mcpb) environment and its Include PDF support "
                   "setting is on, but its PDF install did not complete or does not match this release (the "
                   "extension's log says why; an unpinned install can drift off the release's versions); "
                   "restart the extension to retry it, or install the CLI to index PDFs")
        else:
            why = ("this is the Claude Desktop extension (.mcpb) environment, installed without PDF support; "
                   "turn on its Include PDF support setting (Settings, Connectors, Desktop, Conexus) and "
                   "restart the extension, or install the CLI")
        return InstallPlan("mcpb", False, _cli_install_command(), why)
    return InstallPlan(
        "dev", False, "uv sync",
        "this is a source checkout; its dev group carries the PDF stack",
    )


def repair_command() -> str:
    """The command a person runs to install or repair PDF support on THIS install.

    ``install_plan().command``: ``nx self install --extras pdf`` on a generation,
    plain ``nx self install`` on a legacy uv tool tree (the convergence carries
    ``pdf``; ``--extras`` is refused there, nexus-11x9o), the rebuild script for a
    directory install, and so on. Every message that can reach a non-generation
    install uses this, not :data:`INSTALL_COMMAND`. Never raises: a layout that
    cannot be read falls back to the generation command."""
    try:
        return install_plan().command
    except Exception:  # noqa: BLE001 — a message must still name a command when the layout is unreadable
        return INSTALL_COMMAND


#: The Desktop extension's include_pdf setting as mcpb/src/bootstrap.py receives it (and the server inherits it).
MCPB_PDF_SETTING_ENV: str = "NX_MCPB_INCLUDE_PDF"


def _cli_install_command() -> str:
    """The first-install one-liner for this platform (the installer scripts install PDF support by default)."""
    if os.name == "nt":
        return "irm https://hellblazer.github.io/nexus/install.ps1 | iex"
    return "curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh"


def _running_from_mcpb_bundle(prefix: Path) -> bool:
    """Whether *prefix* (the running venv root) is the Claude Desktop extension's environment.

    ``mcpb/src/bootstrap.py`` runs ``uv sync`` in the bundle directory and then ``uv run --directory
    <bundle> src/server.py``, so the interpreter's ``sys.prefix`` is ``<bundle>/.venv``. The bundle is
    recognised by what it ships beside that venv: ``manifest.json``, ``src/bootstrap.py`` and a
    ``pyproject.toml`` naming ``conexus-mcpb``. A source checkout keeps ``mcpb/`` one level down, so
    its root carries none of the three."""
    root = prefix.parent
    try:
        project = root / "pyproject.toml"
        return (
            (root / "manifest.json").is_file()
            and (root / "src" / "bootstrap.py").is_file()
            and 'name = "conexus-mcpb"' in project.read_text(encoding="utf-8")
        )
    except OSError:
        return False


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
    text_only = text_only_remedy(entry)
    return (
        f"{head}: {entry} needs docling and MinerU, and nothing was written. "
        f"{why + '. ' if why else ''}Run: {plan.command}" + (f"\n{text_only}" if text_only else "")
    )


def missing_message(entry: str = "PDF extraction") -> str | None:
    """The refusal text when the stack is not present, else None. For an error
    that arrives as an ImportError deep in extraction: whether that means "not
    installed" (this text) or "installed and corrupt" (None) is decided here."""
    st = state()
    return None if st.present else _remedy(entry, st, None)


def require(entry: str = "PDF extraction", *, extractor: str = "") -> None:
    """Raise :class:`PdfStackMissingError` unless the stack is present. Never
    installs: the library-level guard (``PDFExtractor.extract``) and any caller
    that must not start a download. The positive verdict is cached. An *extractor* that
    needs no stack (:func:`needs_stack`) returns at once, without reading the state."""
    global _present_cache
    if not needs_stack(extractor) or _present_cache:
        return
    st = state()
    if st.present:
        try:
            _refuse_if_windows_runtime_gap()
        except PdfStackError as exc:
            _with_text_only_remedy(exc, entry)
            raise
        _present_cache = True  # only reached with no gap: a refusal is never cached
        return
    raise PdfStackMissingError(_remedy(entry, st, None))


def _windows_runtime_gap(
    platform: str | None = None,
    config_dir: str | None = None,
    system: str | None = None,
) -> tuple[str, str | None] | None:
    """On Windows, why the stack's extension modules cannot load, else None.

    The stack needs the base pair PLUS ``msvcp140_atomic_wait.dll`` (torch 2.13
    imports it; nexus-vyy65) in System32 or together in one app-local directory.
    ``("none", None)``: no VC++ runtime anywhere. ``("base_only", dir)``: the base
    pair is there (in *dir*) but ``msvcp140_atomic_wait.dll`` is not. None off
    Windows, when the set is present, or when the probe cannot tell (do not
    refuse on a guess)."""
    try:
        from nexus import _vcrt  # noqa: PLC0415 — deferred
        import nexus.config as _config  # noqa: PLC0415 — deferred; module attribute so a patched config dir is seen (nexus-78blw)

        if not _vcrt.is_windows(platform):
            return None
        cfg = config_dir if config_dir is not None else str(_config.nexus_config_dir())
        kind, _where = _vcrt.runtime_source(platform, cfg, system, _vcrt.PDF_STACK_DLLS)
        if kind != _vcrt.MISSING:
            return None
        base_kind, base_where = _vcrt.runtime_source(platform, cfg, system)
    except Exception:  # noqa: BLE001 — cannot tell: do not refuse on a guess
        return None
    if base_kind in (_vcrt.SYSTEM, _vcrt.APP_LOCAL):
        return "base_only", base_where
    return "none", None


def _windows_runtime_missing() -> bool:
    """On Windows, true when the VC++ runtime the stack needs (the base pair and
    ``msvcp140_atomic_wait.dll``) is absent everywhere its extension modules look.
    False elsewhere."""
    return _windows_runtime_gap() is not None


def _gap_sentence(gap: tuple[str, str | None]) -> str:
    """What is missing, as one sentence ending in a space, for the refusal and the
    slim-build line. One wording per cause, so the base-runtime-present case never
    claims the machine has no runtime. It states the machine's state and nothing about any
    engine release, so it is true before and after one carries ``msvcp140_atomic_wait.dll``
    (nexus-vyy65)."""
    kind, where = gap
    if kind == "base_only":
        return (
            f"The Visual C++ runtime found in {where} lacks msvcp140_atomic_wait.dll, "
            "which torch needs. "
        )
    return "This machine has no Microsoft Visual C++ runtime. "


def _runtime_ways() -> str:
    """The two ways to get the missing runtime, as a sentence. Conditional on purpose:
    ``nx init`` and ``nx upgrade`` place ``msvcp140_atomic_wait.dll`` only when the engine
    release this nx pins carries it, and Microsoft's redistributable works with any engine,
    so the sentence is true before and after an engine release that carries the DLL
    (nexus-vyy65)."""
    return (
        "`nx init` or `nx upgrade` places the missing files itself when the engine release "
        f"this nx pins carries them; Microsoft's redistributable ({VCRT_URL}) provides them "
        "with any engine."
    )


def _windows_runtime_refusal(
    plan: InstallPlan, gap: tuple[str, str | None], *, then: str | None = None,
) -> str:
    """The refusal text for a :func:`_windows_runtime_gap`.

    The tail is "and run: <install command>" for a stack that is not installed yet.
    A caller whose stack IS installed (it cannot load) passes *then*, what to do
    after the redistributable, so the text never sends the user to reinstall."""
    tail = f"and run: {plan.command}" if then is None else f"then {then}"
    return (
        f"PDF support needs the Visual C++ runtime. {_gap_sentence(gap)}"
        f"{_runtime_ways()} To use the redistributable, install it from {VCRT_URL} {tail}"
    )


def drop_pdf_on_runtime_gap(
    extras: Sequence[str], *, asked: bool = False,
) -> tuple[list[str], str | None]:
    """The BUILD-SLIM-AND-SAY-SO policy (nexus-vyy65, Sam 2026-10-25).

    ``(extras, line)``: *extras* without ``pdf`` and the one line to show, when *extras*
    names ``pdf`` and, on Windows, the runtime DLLs torch imports are absent; else
    *extras* unchanged and None. Every install and upgrade build runs its resolved extras
    through here, AFTER the merge and the carry-forward, so a machine that cannot load the
    PDF stack still gets the rest of nexus and an upgrade is never frozen by it. The new
    receipt then does not list ``pdf``, so nothing later is gated by it; the first
    ``nx index pdf`` refuses with the link (:func:`ensure`) and installs on demand once the
    runtime is in place. *asked*: the user typed ``--extras pdf``; the line then
    says PDF support will not be installed, rather than that the build leaves it out. The
    line is worded as what will happen: it prints before the build and on ``--dry-run``."""
    if not any(e.strip().lower() == "pdf" for e in extras):
        return list(extras), None
    gap = _windows_runtime_gap()
    if gap is None:
        return list(extras), None
    kept = [e for e in extras if e.strip().lower() != "pdf"]
    plan = install_plan()
    if asked:
        line = (
            f"PDF support will NOT be installed, although --extras pdf asked for it. {_gap_sentence(gap)}"
            f"{_runtime_ways()} Once the runtime is in place, run: {plan.command}"
        )
    else:
        line = (
            f"PDF support will not be installed by this build. {_gap_sentence(gap)}"
            f"It installs on first use once the runtime is in place: {_runtime_ways()}"
        )
    return kept, line


def _refuse_if_windows_runtime_gap() -> None:
    """Raise the refusal when the stack's files are present but, on Windows, the
    runtime DLLs it imports are not (``import torch`` would fail with WinError 126).
    The same text :func:`ensure` gives before an install, minus the install command:
    nothing is missing from the stack, the machine lacks the redistributable. A no-op
    off Windows and when the probe cannot tell."""
    gap = _windows_runtime_gap()
    if gap is not None:
        raise PdfStackMissingError(
            _windows_runtime_refusal(install_plan(), gap, then="run the command again.")
        )


def _require_windows_runtime(plan: InstallPlan) -> None:
    """Refuse, before anything is written, on a Windows box that lacks the VC++
    runtime the stack's extension modules need. The on-demand install
    (:func:`ensure`) runs it, so it never builds a generation whose PDF support would
    then show as broken in ``nx doctor`` (nexus-2k66t.4.7). Builds that are not a PDF
    request (bootstrap, upgrade, rebuild, carry-forward) go slim instead:
    :func:`drop_pdf_on_runtime_gap`."""
    if _windows_runtime_missing():
        gap = _windows_runtime_gap() or ("none", None)
        raise PdfStackMissingError(_windows_runtime_refusal(plan, gap))


def _classify_failure(message: str) -> PdfStackError:
    """Map an installer failure message to the exit-code classes. Reuses
    ``self_cmd``'s diagnosis so the network and TLS wording stays one rule."""
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

    if self_cmd._NETWORK_FAILURE_RE.search(message) or self_cmd._TLS_FAILURE_RE.search(message):  # noqa: SLF001
        return PdfStackNetworkError(
            "PDF support could not be downloaded (network or TLS failure); nothing was written. "
            f"Retry when the network is back, or run `{INSTALL_COMMAND}` ahead of time.\n{message}"
        )
    # The build's advice (the cu126 hint for a cu128 backend, the macOS floor) is written at
    # the HEAD of the message and uv's own text follows it; a tail alone drops the advice and
    # leaves the person with uv's bare "unsatisfiable" (nexus-husxe). Keep the advice whole
    # and tail only uv's output.
    advice, uv_output = self_cmd.split_build_failure(message)
    tail = "\n".join((uv_output or message).strip().splitlines()[-12:])
    lead = f"{advice}\n\n" if advice else ""
    return PdfStackInstallError(
        f"PDF support could not be installed; nothing was written.\n{lead}{tail}"
    )


@contextlib.contextmanager
def _install_lock(doing: str = "installing PDF support"):
    """Serialize concurrent fetches. The waiter re-checks the state after it
    gets the lock, so two PDF indexes started together fetch once. *doing* names
    the build that holds the lock, for the line a waiter prints."""
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
        try:
            _locking.lock_fd(fd, blocking=False)
        except BlockingIOError:
            # A peer is mid-install: a waiter would otherwise sit silent for
            # minutes (nexus-2k66t.4.8). Say so once, then block.
            _say(f"Another process is {doing}; waiting for it to finish.")
            _locking.lock_fd(fd, blocking=True)
        yield
    finally:
        with contextlib.suppress(OSError):
            _locking.unlock_fd(fd)
        os.close(fd)


def _install_generation(*, carrying: bool = False, notes: list[str] | None = None) -> None:
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
    # nexus-p4vhj: a carry-forward on a Windows box without the VC++ runtime builds slim
    # and says so (drop_pdf_on_runtime_gap); announcing a download first would be false.
    if not (carrying and _windows_runtime_gap() is not None):
        _say(notice)
    # perform_self_install reports its reap on stdout; this command's stdout is
    # its own (a caller may parse it), so the install's chatter goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        # A carry-forward is the upgrade's own rule, not a flag the user typed: on a machine
        # without the runtime it builds slim and reports through *notes* (nexus-vyy65).
        slim: dict = {"pdf_asked": False} if carrying else {}
        if notes is not None:
            slim["pdf_note"] = notes
        self_cmd.perform_self_install(add_extras=extras, version=version, progress=True, **slim)


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
    code otherwise; every such error names the text-only way out for *entry*
    (:func:`text_only_remedy`).
    """
    try:
        return _ensure(entry)
    except PdfStackError as exc:
        _with_text_only_remedy(exc, entry)
        raise


def _ensure(entry: str) -> Outcome:
    global _present_cache
    st = state()
    if st.present:
        _refuse_if_windows_runtime_gap()
        _present_cache = True  # only reached with no gap: a refusal is never cached
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
    _require_windows_runtime(plan)
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


def ensure_or_exit(
    entry: str, *, hold_on_refuse: bool = False, defer_rerun: bool = False, quiet_refusal: bool = False,
    extractor: str = "",
) -> bool:
    """The CLI form of :func:`ensure`. Returns True to go on. Exits the process
    with the right status on a failure, or with the child's status after a
    re-run in the new generation. With *hold_on_refuse*, a REFUSAL returns
    False instead of exiting (``nx index repo`` indexes code and prose and holds
    the PDFs back); network and installer failures still exit. With
    *defer_rerun*, an install raises :class:`RerunRequired` for the CLI command
    to catch after it has released its locks, instead of re-running here. With
    *quiet_refusal*, a held refusal says nothing (the caller already printed it
    once this run). An *extractor* that needs no stack (:func:`needs_stack`, the explicit
    ``pymupdf``) returns True at once: nothing is checked, installed or refused."""
    if not needs_stack(extractor):
        return True
    try:
        outcome = ensure(entry)
    except PdfStackMissingError as exc:
        if hold_on_refuse:
            if not quiet_refusal:
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


def _carry_failed_sentinel() -> Path | None:
    """The marker a failed carry-forward leaves (and a success removes), or None
    when the config directory cannot be named."""
    try:
        import nexus.config as _config  # noqa: PLC0415 — deferred; module attribute so a patched config dir is seen (nexus-78blw)

        return _config.nexus_config_dir() / _CARRY_FAILED_SENTINEL
    except Exception:  # noqa: BLE001 — no config dir: no marker
        return None


def carry_forward_note() -> str | None:
    """One sentence for `nx doctor` when an upgrade owes this install PDF support
    (nexus-2k66t.4.9, F12): the last carry-forward failed (and when), or it has not
    run yet. None when neither holds. A failure outranks a pending run: it is the
    reason the run has not finished. Both need a carry to be owed: a failed marker
    left by an old attempt says nothing on an install that no longer owes one (a
    deliberately slim box, or one that carried PDF support forward since), so it
    would warn for as long as the file lasts."""
    if not carry_forward_pending():
        return None
    sentinel = _carry_failed_sentinel()
    if sentinel is not None:
        try:
            when = sentinel.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            when = None
        except OSError:
            when = ""
        if when is not None:
            stamp = f" ({when})" if when else ""
            return (
                f"the last upgrade could not carry PDF support forward{stamp}; this install keeps working "
                f"without it. Retry: {INSTALL_COMMAND}"
            )
    return (
        "this install was built before PDF support became an extra and has not carried it forward yet; "
        f"`nx upgrade` does it, or run {INSTALL_COMMAND}"
    )


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
    sentinel = _carry_failed_sentinel()
    if auto_mode and sentinel is not None:
        try:
            age = time.time() - sentinel.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < CARRY_FORWARD_AUTO_BACKOFF_S:
            return f"PDF support carry-forward deferred: the last attempt failed {int(age)}s ago"
    notes: list[str] = []
    try:
        with _install_lock():
            # A peer's carry (the lockstep upgrade, a SessionStart --auto, a
            # pre-change hook) may have built it while we waited.
            if current_generation_has_stack():
                return "PDF support was already carried forward into the current generation"
            _install_generation(carrying=True, notes=notes)
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
    if notes:
        # nexus-vyy65: the build was slim because this machine cannot load the PDF stack. The
        # generation is current and its receipt no longer predates the split, so this does not
        # repeat; the first `nx index pdf` installs the stack once the runtime is present.
        return notes[0]
    return "PDF support carried forward into a new generation (existing installs keep it)"


# ── a generation built without the release's dependency lock (nexus-au0s3) ──
#
# WHICH BUILDS THIS COVERS. The hop out of 7.85.0 is NOT this rung's: that release's
# receipts predate the PDF split, so the carry-forward above makes the one build there
# (and that build, by the running installer, applies the lock). This rung covers a later
# generation that was built without the lock: an installer fallback, or a package that
# shipped no constraints file at the time. It never touches a generation built unpinned on
# purpose (NX_INSTALL_UNPINNED=1, an operator taking a security fix); the next plain
# `nx self install` pins again, as the docs say.

#: A separate marker from the carry-forward's: a failure here is not "PDF support could
#: not be carried", and `nx doctor` reads each for its own sentence.
_REBUILD_FAILED_SENTINEL = "installer-rebuild-failed"

REBUILD_RETRY_COMMAND = "nx upgrade"

REBUILD_NOTICE = (
    "Rebuilding the current generation of conexus {version} with the release's dependency lock: "
    "it was built without it, so its dependencies were resolved fresh from the package index. "
    "The download is a full install; the old generation is kept for rollback."
)


class InstallerRebuildError(RuntimeError):
    """The rebuild ran and current still does not hold the lock."""


def _rebuild_failed_sentinel() -> Path | None:
    """The marker a failed rebuild leaves (and a success removes), or None when the
    config directory cannot be named."""
    try:
        import nexus.config as _config  # noqa: PLC0415 — deferred; module attribute so a patched config dir is seen

        return _config.nexus_config_dir() / _REBUILD_FAILED_SENTINEL
    except Exception:  # noqa: BLE001 — no config dir: no marker
        return None


def _same_version(a: str, b: str) -> bool:
    from packaging.version import InvalidVersion, Version  # noqa: PLC0415 — deferred

    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _package_ships_constraints() -> bool:
    """Whether the RUNNING package carries the lock a rebuild would apply."""
    return constraints_path().is_file()


def _stale_current_extras() -> tuple[str, ...] | None:
    """The extras of the generation ``current`` names when the running installer
    owes it a rebuild, else None.

    Owed when ALL hold: ``current``'s receipt is readable, came from the registry
    (a checkout build is rebuilt by ``reinstall-tool.sh``), names the version this
    process runs (an older ``current`` is the ordinary upgrade's to replace, a
    newer one was built by something newer than this process), the receipt says the
    build applied NO lock (``none``, or nothing recorded by an installer that did
    not know the field), and the running package ships a lock to apply. Never
    ``unpinned`` (a deliberate opt-out) and never when this process has opted out."""
    if os.environ.get("NX_INSTALL_UNPINNED") == "1" or not _package_ships_constraints():
        return None
    found = _current_generation_receipt()
    if found is None:
        return None
    _gen, receipt = found
    from nexus import install_layout  # noqa: PLC0415 — deferred

    if receipt.source_kind != "registry" or receipt.constraints not in (
        install_layout.CONSTRAINTS_NONE, install_layout.CONSTRAINTS_UNKNOWN,
    ):
        return None
    # A current that predates the PDF split is the carry-forward's to rebuild (it
    # adds pdf); a pdf-less rebuild here would stamp a post-split receipt and the
    # stack would never be carried. installer_rebuild_pending checks the RUNNING
    # receipt for that; this checks current's own.
    if install_layout.receipt_predates_pdf_split(receipt):
        return None
    try:
        if not _same_version(receipt.version, _running_version()):
            return None
    except Exception:  # noqa: BLE001 — no readable running version: no claim
        return None
    return tuple(receipt.extras)


def installer_rebuild_pending() -> bool:
    """True when ``current`` was built without the release's lock and nothing else
    will rebuild it.

    Not while the PDF carry-forward is owed: that one build is also made by the
    running installer, and a pdf-less rebuild first would stamp a receipt that no
    longer predates the split, so the stack would never be carried. Only a
    generation install the auto path may touch (:func:`install_plan`) qualifies."""
    if carry_forward_pending():
        return False
    if _stale_current_extras() is None:
        return False
    plan = install_plan()
    return plan.kind == "generation" and plan.auto


def installer_rebuild_note() -> str | None:
    """One sentence for `nx doctor` when the rebuild is owed: the last attempt failed
    (and when), or it has not run yet. None when nothing is owed, so a virgin box, a
    rebuilt one and a deliberately unpinned one are silent, and an old failed marker
    says nothing once the debt is gone."""
    try:
        if not installer_rebuild_pending():
            return None
    except Exception:  # noqa: BLE001 — a doctor row never raises
        return None
    sentinel = _rebuild_failed_sentinel()
    if sentinel is not None:
        try:
            when = sentinel.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            when = None
        except OSError:
            when = ""
        if when is not None:
            stamp = f" ({when})" if when else ""
            return (
                f"the last attempt to rebuild the current generation with the release's dependency lock failed{stamp}; "
                f"it keeps working as built. Retry: {REBUILD_RETRY_COMMAND}"
            )
    return (
        "the current generation was built without the release's dependency lock, so its dependencies were resolved "
        f"fresh from the package index; `nx upgrade` rebuilds it, or run {REBUILD_RETRY_COMMAND}"
    )


def _current_lock_applied() -> bool:
    from nexus import install_layout  # noqa: PLC0415 — deferred

    found = _current_generation_receipt()
    return found is not None and found[1].constraints == install_layout.CONSTRAINTS_APPLIED


def _rebuild_generation(extras: tuple[str, ...], *, after_flip=None) -> None:
    """Build a generation at the running version with the running installer and
    EXACTLY *extras* (current's, not the host's), flip to it, reap by the normal
    keep-3 rule. No version change, no extra added or dropped: the build differs
    from ``current`` only in how it was resolved.

    *after_flip* is called with the new generation after the flip and BEFORE the
    reap (nexus-ff366), so a check that fails can put the old pointers back while
    the generation ``previous`` used to name still exists."""
    from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

    version = _running_version()
    _say(REBUILD_NOTICE.format(version=version))
    with contextlib.redirect_stdout(sys.stderr):
        self_cmd.perform_self_install(
            exact_extras=extras, version=version, progress=True, after_flip=after_flip,
        )


def rebuild_stale_installer(*, auto_mode: bool = False) -> str | None:
    """Rebuild ``current`` when :func:`installer_rebuild_pending`. Returns a
    one-line account, or None when there was nothing to do. Never raises: a
    failed build leaves ``current`` as it was (the build flips only on success), a
    failed post-check puts it back (below), either is reported in the line, and
    is retried by the next run, backed off under
    ``--auto`` by a marker of its own (same window as the carry-forward's) so a
    box that cannot reach the index does not refetch at every session start.

    A build that returns without raising is not yet a success: ``current``'s
    receipt must now say the lock was applied. A build that leaves it unpinned (an
    installer fallback, an index serving a different wheel) is a failure, so it cannot be
    reported as done and repeated at every session start, and ``current`` and
    ``previous`` are put back where they were before the build flipped them
    (nexus-ff366): the new generation is no better than the one it replaced, and
    keeping it as ``current`` would let each retry's keep-3 reap the original."""
    if not installer_rebuild_pending():
        return None
    sentinel = _rebuild_failed_sentinel()
    if auto_mode and sentinel is not None:
        try:
            age = time.time() - sentinel.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < CARRY_FORWARD_AUTO_BACKOFF_S:
            return f"installer rebuild deferred: the last attempt failed {int(age)}s ago"
    moved_back: list[str] = []
    slim_note: str | None = None
    try:
        with _install_lock("rebuilding the current generation"):
            # A peer's run may have rebuilt it while we waited; `current` is the
            # only thing that says so.
            extras = _stale_current_extras()
            if extras is None:
                return "the current generation was already rebuilt with the release's dependency lock"
            # nexus-vyy65: on a machine that cannot load the PDF stack the rebuild is slim, like
            # every other build; dropping pdf HERE (not inside the install) puts the one line in
            # this function's account. `current` is still rebuilt with the lock.
            extras_list, slim_note = drop_pdf_on_runtime_gap(extras)
            extras = tuple(extras_list)
            replaced, replaced_previous = _generation_pointers()

            def check(_generation: Path) -> None:
                """Runs after the flip and before the reap: the build is only a success if
                `current` now says the lock was applied, else the flip is undone."""
                if not _current_lock_applied():
                    moved_back.append(_put_current_back(replaced, replaced_previous))
                    raise InstallerRebuildError(
                        "the rebuilt generation does not record the release's dependency lock as applied"
                    )

            _rebuild_generation(extras, after_flip=check)
    except Exception as exc:  # noqa: BLE001 — reported, retried next run
        if sentinel is not None:
            with contextlib.suppress(OSError):
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n", encoding="utf-8")
        _log.warning("installer_rebuild_failed", error=str(exc))
        why = "the build did not apply the lock" if isinstance(exc, InstallerRebuildError) else type(exc).__name__
        return (
            f"the current generation could not be rebuilt with the release's dependency lock ({why}); "
            f"it keeps working as built{''.join(moved_back)}. Retry: {REBUILD_RETRY_COMMAND}"
        )
    if sentinel is not None:
        with contextlib.suppress(OSError):
            sentinel.unlink(missing_ok=True)
    done = "rebuilt the current generation with the release's dependency lock"
    return f"{done}. {slim_note}" if slim_note else done


def _generation_pointers() -> tuple[Path | None, str | None]:
    """``(what current names, what previous names)`` before a build flips them; the
    first is None when ``current`` names no existing directory, which leaves nothing
    to put back."""
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred
        from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

        tools = install_layout.tools_dir()
        return (
            self_cmd._current_generation_dir(tools),  # noqa: SLF001 — same module family
            self_cmd._previous_target(tools),  # noqa: SLF001
        )
    except Exception:  # noqa: BLE001 — unreadable pointers: no claim, no rollback
        return None, None


def _put_current_back(replaced: Path | None, replaced_previous: str | None) -> str:
    """Re-point ``current`` and ``previous`` at what the failed build's flip replaced, and
    return the words for the account: "" when there was nothing to undo, a clause saying what
    the pointers are NOW otherwise. Never raises. The words come from re-reading both pointers
    after the attempt, never from the attempt's own success: a rollback that moved ``current``
    and then failed to restore ``previous`` says so, and one that could not move ``current``
    says it stays on the rebuilt generation (which exists and works)."""
    if replaced is None:
        return ""
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred
        from nexus.commands import self_cmd  # noqa: PLC0415 — deferred

        tools = install_layout.tools_dir()
        if self_cmd._current_generation_dir(tools) == replaced:  # noqa: SLF001
            return ""  # the build never flipped: nothing to undo
        try:
            self_cmd.restore_current_generation(replaced, previous=replaced_previous)
        except Exception as exc:  # noqa: BLE001 — what is left is read back below
            _log.warning("installer_rebuild_rollback_failed", error=str(exc), generation=str(replaced))
        now = self_cmd._current_generation_dir(tools)  # noqa: SLF001
        if now != replaced:
            return f"; current could not be moved back to {replaced.name} and stays on the rebuilt generation"
        platform = self_cmd._plat()  # noqa: SLF001
        previous_now = self_cmd._previous_target(tools)  # noqa: SLF001
        same = (
            previous_now is None if replaced_previous is None
            else previous_now is not None and install_layout.compare_key(
                previous_now, platform=platform,
            ) == install_layout.compare_key(replaced_previous, platform=platform)
        )
        if same:
            return f"; current is back on {replaced.name}"
        was = "no rollback target" if replaced_previous is None else Path(replaced_previous).name
        names = "no rollback target" if previous_now is None else Path(previous_now).name
        return (
            f"; current is back on {replaced.name}, but previous was {was} before the build "
            f"and is now {names}"
        )
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        _log.warning("installer_rebuild_rollback_failed", error=str(exc), generation=str(replaced))
        return "; current may not have been moved back: run `nx doctor`"
