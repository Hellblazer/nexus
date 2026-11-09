# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a bench result was measured on, and the refusal to compare across it (nexus-3ceda.11).

A latency is only a number on one machine against one engine. A report from a 16-core
lab-run grant and one from the laptop, or one against a released engine and one against a
local build, differ for reasons that have nothing to do with the change under test, and
comparing them reads noise as a regression or a win. So every report carries a manifest,
and :func:`comparable` refuses two reports whose manifests differ on what decides the
numbers (:data:`COMPARED_KEYS`). The git sha is recorded but not compared: it is usually
what the comparison is about. It is the Python checkout's sha, not the engine's.

Two limits the keys cannot see: every engine without a release stamp reads ``unstamped``
(two different local builds, or no engine answering at all, compare as equal), and the git
dirty flag ignores untracked files. For an A/B against a local engine, run both arms in one
``--pairs`` run, where both meet the same engine by construction.

The host is described by class, never by name: reports under ``scripts/bench/baselines/``
are committed, and the repository stays scrubbed of host names.
"""
from __future__ import annotations

import datetime as _dt
import os
import platform
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: The manifest keys two results must share to be compared.
COMPARED_KEYS: tuple[str, ...] = ("host_class", "cpu_model", "engine_build")


class ManifestMismatch(ValueError):
    """Two results were measured on different hosts or engines."""


def host_class(env: Mapping[str, str] | None = None) -> str:
    """The host's class: its lab-run grant (``LAB_CPUS``/``LAB_MEM_MB``) when there is one,
    else ``unmanaged`` with the visible cores; with the OS and architecture either way.

    lab-run tells the command what it was given but not the class name, so the grant's size
    stands for it (lab-m is 8c/16384M, lab-l 16c/49152M).
    """
    env = os.environ if env is None else env
    system = f"{platform.system().lower()}-{platform.machine().lower()}"
    cpus, mem = (env.get("LAB_CPUS") or "").strip(), (env.get("LAB_MEM_MB") or "").strip()
    if cpus.isdigit() and mem.isdigit():
        return f"lab-run {cpus}c/{mem}M {system}"
    return f"unmanaged {os.cpu_count() or 0}c {system}"


def cpu_model() -> str:
    """The CPU's model name (``/proc/cpuinfo`` on Linux, ``machdep.cpu.brand_string`` on
    macOS), or ``unknown``. Two lab-run grants of one size on different hosts differ here,
    which the grant size alone cannot show."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                             text=True, check=True, timeout=10).stdout.strip()
        return out or "unknown"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return platform.processor() or "unknown"


def engine_build(probe: Callable[[], tuple[int, int, int] | None] | None = None) -> str:
    """The engine this process talks to: ``release X.Y.Z`` when its ``/version`` carries a
    release stamp, else ``unstamped`` (a local build, or no engine answering)."""
    if probe is None:
        from nexus.db.serving_engine_version import probe_serving_engine_version  # noqa: PLC0415 -- the engine probe imports the HTTP client; a test passes its own probe

        probe = probe_serving_engine_version
    version = probe()
    return "unstamped" if version is None else "release " + ".".join(map(str, version))


def _git(root: Path, *args: str) -> str | None:
    """A git answer, or None where there is none (no checkout, as in a ``lab-remote --no-git``
    copy; no git; a repository git refuses as another user's)."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def build_manifest(
    root: Path, *, env: Mapping[str, str] | None = None,
    probe: Callable[[], tuple[int, int, int] | None] | None = None,
) -> dict[str, Any]:
    """The manifest for a result measured now, from the checkout at *root*. Outside a
    checkout the sha is ``unknown`` and the dirty flag ``None``: neither is a compared key,
    so the result still compares with one from the same host and engine."""
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    return {
        "git_sha": _git(root, "rev-parse", "HEAD") or "unknown",
        "git_dirty": None if status is None else bool(status),
        "host_class": host_class(env),
        "cpu_model": cpu_model(),
        "engine_build": engine_build(probe),
        "python": platform.python_version(),
        "measured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }


def comparable(a: Mapping[str, Any], b: Mapping[str, Any]) -> None:
    """Raise :class:`ManifestMismatch` unless *a* and *b* (two reports, or two manifests)
    were measured on the same host class against the same engine build. A report with no
    manifest cannot be shown to match anything, so it is refused too."""
    ma, mb = a.get("manifest", a), b.get("manifest", b)
    missing = [name for name, m in (("first", ma), ("second", mb))
               if not isinstance(m, Mapping) or not all(k in m for k in COMPARED_KEYS)]
    if missing:
        raise ManifestMismatch(f"the {' and '.join(missing)} result has no manifest; it cannot be compared")
    differ = {k: (ma[k], mb[k]) for k in COMPARED_KEYS if ma[k] != mb[k]}
    if differ:
        detail = "; ".join(f"{k}: {x!r} vs {y!r}" for k, (x, y) in differ.items())
        raise ManifestMismatch(f"results measured on different hosts or engines ({detail}); measure both on one")
