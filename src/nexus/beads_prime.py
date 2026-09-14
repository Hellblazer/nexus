# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""User-level ``beads`` PRIME.md management (nexus-cnzei.8).

``bd`` (https://github.com/BeadsProject/beads) resolves the workflow-priming
text ``bd prime`` prints, in order: a repo-level ``.beads/PRIME.md``, then
``<beads-dir>/PRIME.md``, then a machine-wide file at the OS user config
directory (Go's ``os.UserConfigDir()``: macOS
``~/Library/Application Support/beads/PRIME.md``, Linux
``$XDG_CONFIG_HOME/beads/PRIME.md`` or ``~/.config/beads/PRIME.md``,
Windows ``%AppData%/beads/PRIME.md``). A repo-level file (this repo's own
``.beads/PRIME.md``, nexus-cnzei.2) fixes only that one repo; every OTHER
beads repo on the machine, for every conexus user, still gets ``bd``'s own
bundled PRIME text -- several KB, and on this project's own reading it
contradicts this project's workflow (a conservative no-commit/no-push
default, "do NOT use MEMORY.md", ``bd remember``, ``git add .``).

This module installs a GENERIC, conexus-managed, marker-versioned
``PRIME.md`` at that user-level path when beads is detected, so every beads
repo on the machine gets consistent guidance without a per-repo file.
Confirmed by direct experiment (not merely inspection): ``bd prime`` prints
a PRIME.md file's content byte-for-byte, HTML comment lines included -- the
first-line marker survives as ordinary literal text, harmless in model
context.

MARKER FORMAT (fix round, nexus-cnzei.8 critic pass): the first line is
``<!-- conexus-managed beads PRIME v{version} sha256:{hex64} -->``, where
the hash covers the BODY (everything after that first line) as installed.
This closes the original ship-blocker: a body edited by a human while
leaving the marker line intact used to be classified the same as a
pristine install (byte-compared against the current template) and silently
overwritten. Now, ``status()`` recomputes the body's hash and compares it
against the one recorded in the marker -- a mismatch means a human touched
it since the last install, and it is treated exactly like a file with no
marker at all: :data:`PrimeStatus.USER_AUTHORED`, never auto-overwritten.

LIMITS, stated rather than left implicit:

* This is a MACHINE-WIDE file. Installing it affects every beads repo on
  this box, not only the current project.
* beads 1.2.x still APPENDS ``bd remember`` memories after the PRIME.md
  text (``--no-memories`` suppresses it; ``bd prime`` is not invoked with
  that flag by this module or by the beads Claude Code plugin's own hook).
  Installing this file does not stop that.
* A repo-level ``.beads/PRIME.md`` always wins over this file -- ``bd``
  reads repo-level first.

OPT-OUT: pass ``disabled=True`` to :func:`install_and_describe` (the
``--no-beads-prime`` flag on ``nx init``/``nx upgrade``), or persist
``beads_prime.manage: false`` via ``nx config set beads_prime.manage
false`` (checked by :func:`manage_enabled`). Either wins outright -- no
detection, no write, no doctor suggestion to reconsider. Deleting an
installed file also works as an implicit, permanent opt-out for that one
file: it falls out of every ``MANAGED_*`` state into ``ABSENT`` (which
`install()` will recreate on the next run) -- an editor who wants to STOP
management for good should also flip the config key or pass the flag,
not just delete the file once.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import structlog

_log = structlog.get_logger()

__all__ = [
    "InstallOutcome",
    "PrimeStatus",
    "beads_detected",
    "install",
    "install_and_describe",
    "load_template",
    "manage_enabled",
    "status",
    "user_prime_path",
]

#: First-line marker every conexus-managed PRIME.md carries: version plus a
#: sha256 of the body that follows it. Anything else (no marker at all, an
#: unparseable one, or a recorded hash that no longer matches the body) is
#: treated as user-authored and never touched.
_MARKER_RE = re.compile(
    r"^<!-- conexus-managed beads PRIME v(?P<version>\d+)"
    r" sha256:(?P<hash>[0-9a-f]{64}) -->$"
)
_TEMPLATE_RESOURCE = "beads_prime_template.md"

#: Persistent opt-out config key: ``nx config set beads_prime.manage false``.
_MANAGE_CONFIG_SECTION = "beads_prime"
_MANAGE_CONFIG_KEY = "manage"

#: Human-readable undo/disclosure text, shared by the CLI one-liner and the
#: doctor row's fix suggestion so the two surfaces never say different
#: things (critic finding: disclosure was previously stated in only one of
#: docs/CLI/doctor, never all three).
UNDO_HINT = (
    "This is machine-wide -- it affects every beads repo on this machine, "
    "not just this one. Delete the file to restore bd's own default, or "
    "stop future writes with --no-beads-prime "
    "(or `nx config set beads_prime.manage false`)."
)


class PrimeStatus(str, Enum):
    """The four states a user-level ``PRIME.md`` can be in."""

    ABSENT = "absent"
    MANAGED_CURRENT = "managed-current"
    MANAGED_STALE = "managed-stale"
    USER_AUTHORED = "user-authored"


@dataclass(frozen=True)
class InstallOutcome:
    """What :func:`install` did.

    ``action`` is one of ``"installed"``, ``"updated"``, ``"up to date"``,
    ``"left alone (user-authored)"``, or ``"left alone (installed is
    newer)"``. ``backup_path`` is set only for ``"updated"`` -- the
    previous managed content, preserved before being overwritten.
    """

    action: str
    path: Path
    backup_path: Path | None = None


def user_prime_path(
    *,
    platform: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The OS user-config path ``bd``'s final fallback tier resolves,
    matching Go's ``os.UserConfigDir()``:

    * macOS (``darwin``) -- ``~/Library/Application Support/beads/PRIME.md``
    * Windows (``win32``) -- ``%AppData%/beads/PRIME.md``; when ``APPDATA``
      is unset (Go's function would error there -- a caller here needs a
      path, not an exception) falls back to ``~/AppData/Roaming``.
    * everything else (Linux and other Unix-likes) --
      ``$XDG_CONFIG_HOME/beads/PRIME.md``, or ``~/.config/beads/PRIME.md``
      when that variable is unset.

    *platform*, *home*, and *environ* are each injectable for tests and
    default to the real ambient values (:data:`sys.platform`,
    :meth:`Path.home`, :data:`os.environ`) when omitted. The unit suite
    fences the NO-ARGS call path to a per-test tmp dir
    (``tests/conftest.py::_fence_beads_prime_user_path``); passing any of
    these three kwargs explicitly bypasses that fence by design, so a test
    of THIS function's own per-platform logic exercises the real
    implementation against its own injected fixture.
    """
    plat = platform if platform is not None else sys.platform
    env = environ if environ is not None else os.environ
    base_home = home if home is not None else Path.home()

    if plat == "darwin":
        base = base_home / "Library" / "Application Support"
    elif plat == "win32":
        appdata = env.get("APPDATA")
        base = Path(appdata) if appdata else base_home / "AppData" / "Roaming"
    else:
        xdg = env.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else base_home / ".config"
    return base / "beads" / "PRIME.md"


def _default_claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def beads_detected(
    *,
    which: Callable[[str], str | None] = shutil.which,
    claude_config_dir: Path | None = None,
) -> tuple[bool, str]:
    """Is beads present on this machine? Returns ``(present, reason)``.

    Two independent signals, either sufficient:

    1. ``bd`` on ``PATH`` -- the CLI itself.
    2. The beads Claude Code plugin, installed under either of the two
       real layouts Claude Code's own puller produces (verified against a
       live install, nexus-cnzei.8 CRE fix round -- the original globs
       matched neither):

       * marketplace clone: ``<claude-config>/plugins/marketplaces/<name>/
         plugins/beads/.claude-plugin/plugin.json`` -- the marketplace
         REPO checked out under its own name, with a ``plugins/`` segment
         before the plugin directory (this is a plain git checkout of the
         marketplace's OWN repo layout, not a Claude-Code-specific one).
       * version-pinned cache: ``<claude-config>/plugins/cache/<name>/
         beads/<version>/.claude-plugin/plugin.json`` -- no ``plugins/``
         segment here; the cache flattens straight to ``<plugin>/<version>``.

       Honours ``CLAUDE_CONFIG_DIR`` the same way :mod:`nexus.health`'s
       Claude settings resolution does, when *claude_config_dir* is not
       passed explicitly.

    A user who only has the plugin (no local ``bd`` binary yet) still
    benefits once they install ``bd`` -- the file will already be there.
    A user who only has the binary still gets the file ``bd`` itself reads.
    """
    bd_path = which("bd")
    if bd_path:
        return True, f"bd on PATH ({bd_path})"

    base = claude_config_dir if claude_config_dir is not None else _default_claude_config_dir()
    plugins_root = base / "plugins"

    marketplaces_root = plugins_root / "marketplaces"
    if marketplaces_root.is_dir() and any(
        marketplaces_root.glob("*/plugins/beads/.claude-plugin/plugin.json")
    ):
        return True, f"beads plugin found under {marketplaces_root}"

    cache_root = plugins_root / "cache"
    if cache_root.is_dir() and any(
        cache_root.glob("*/beads/*/.claude-plugin/plugin.json")
    ):
        return True, f"beads plugin found under {cache_root}"

    return False, "bd not on PATH and no beads Claude Code plugin found"


def load_template() -> str:
    """The packaged, generic ``PRIME.md`` text this module installs.

    Resolved through :mod:`importlib.resources` so it works from a wheel
    install and from an editable checkout alike (the same approach
    :func:`nexus.commands.self_cmd.packaged_install_dir` and
    :func:`nexus.tables.load` use for their own packaged data).
    """
    from importlib.resources import files  # noqa: PLC0415 — deferred, stdlib

    return (files("nexus") / _TEMPLATE_RESOURCE).read_text(encoding="utf-8")


def _split_first_line(text: str) -> tuple[str, str]:
    """*(first_line, body)* -- *body* is everything after the first line's
    own trailing newline (never re-including it).
    """
    first, _, body = text.partition("\n")
    return first, body


def _parse_marker(first_line: str) -> tuple[int, str] | None:
    """*(version, recorded_hash)* from *first_line*, or ``None`` when it
    does not carry a recognizable conexus-managed marker at all.
    """
    m = _MARKER_RE.match(first_line.rstrip("\n"))
    if m is None:
        return None
    return int(m.group("version")), m.group("hash")


def _hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _template_marker() -> tuple[int, str]:
    """The packaged template's own *(version, recorded_hash)* -- a load-
    bearing self-consistency invariant (the packaged file's declared hash
    must equal its actual body's hash) pinned by
    ``tests/test_beads_prime.py::TestPackagedTemplate::
    test_marker_hash_is_self_consistent``, not re-verified on every call
    here for cost reasons.
    """
    first_line, _body = _split_first_line(load_template())
    parsed = _parse_marker(first_line)
    if parsed is None:
        raise ValueError(
            "packaged beads_prime_template.md has no valid conexus-managed "
            "marker on its first line -- this is a packaging defect"
        )
    return parsed


def manage_enabled() -> bool:
    """Whether the persisted opt-out (``nx config set beads_prime.manage
    false``) allows this module to detect/install at all.

    Best-effort: any failure reading config is treated as "not declined"
    (``True``) -- a config-read hiccup must never silently disable a
    feature the user never explicitly opted out of.
    """
    try:
        from nexus.config import load_config  # noqa: PLC0415 — deferred, avoids CLI-cold-start cost
        value = load_config().get(_MANAGE_CONFIG_SECTION, {}).get(_MANAGE_CONFIG_KEY, True)
    except Exception:  # noqa: BLE001 — best-effort: an unreadable config is not a decline
        return True
    return value is not False


def status(path: Path) -> PrimeStatus:
    """Classify *path* into one of the four :class:`PrimeStatus` states.

    Never raises. A path that does not exist at all is :data:`ABSENT`. Any
    OTHER failure to read it (a directory, permission denied, non-UTF-8
    bytes) is reported :data:`USER_AUTHORED` -- the safe default, since it
    guarantees :func:`install` never writes over something it could not
    first inspect.

    A recognizable marker whose recorded hash no longer matches the body's
    actual hash means a human edited the body while leaving the marker
    line intact -- also :data:`USER_AUTHORED` (fix round: this used to be
    silently classified :data:`MANAGED_STALE` and overwritten on the next
    install, the ship-blocker the hash exists to close). Only when the
    recorded hash STILL matches is the file genuinely ours to manage, and
    :data:`MANAGED_CURRENT` vs :data:`MANAGED_STALE` is then a plain
    byte-for-byte compare against :func:`load_template`.
    """
    if not path.exists():
        return PrimeStatus.ABSENT
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return PrimeStatus.USER_AUTHORED

    first_line, body = _split_first_line(text)
    parsed = _parse_marker(first_line)
    if parsed is None:
        return PrimeStatus.USER_AUTHORED
    _version, recorded_hash = parsed
    if _hash_body(body) != recorded_hash:
        return PrimeStatus.USER_AUTHORED

    return (
        PrimeStatus.MANAGED_CURRENT
        if text == load_template()
        else PrimeStatus.MANAGED_STALE
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path*, replacing it atomically (write-to-temp +
    ``os.replace``) so a crash mid-write never leaves a truncated
    ``PRIME.md`` for ``bd`` to read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".prime-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _backup_path_for(target: Path) -> Path:
    return target.with_name(target.name + ".bak")


def install(path: Path | None = None) -> InstallOutcome:
    """Install or refresh the user-level ``PRIME.md`` at *path* (default
    :func:`user_prime_path`).

    Idempotent: a second call once the file is current is a no-op. Never
    overwrites a user-authored file, in either direction. Before replacing
    an existing MANAGED file, the previous content is backed up to a
    single rolling ``<name>.bak`` sibling (overwritten each time, not
    timestamped -- one backup generation, not an unbounded pile). A
    managed file whose recorded version is NEWER than the packaged
    template's (a downgrade -- an older conexus install running against a
    file a newer one wrote) is left alone rather than regressed backward.

    Does NOT consult :func:`manage_enabled` or a disable flag -- that
    policy decision belongs to the caller (see
    :func:`install_and_describe`); this function always does the concrete
    filesystem thing its name says, unconditionally.
    """
    target = path if path is not None else user_prime_path()
    current = status(target)
    if current is PrimeStatus.USER_AUTHORED:
        return InstallOutcome("left alone (user-authored)", target)
    if current is PrimeStatus.MANAGED_CURRENT:
        return InstallOutcome("up to date", target)

    template = load_template()
    if current is PrimeStatus.MANAGED_STALE:
        installed_text = target.read_text(encoding="utf-8")
        installed_marker = _parse_marker(_split_first_line(installed_text)[0])
        template_version, _ = _template_marker()
        if installed_marker is not None and installed_marker[0] > template_version:
            return InstallOutcome("left alone (installed is newer)", target)
        backup = _backup_path_for(target)
        backup.write_text(installed_text, encoding="utf-8")
        _atomic_write(target, template)
        _log.info("beads_prime_install", action="updated", path=str(target), backup=str(backup))
        return InstallOutcome("updated", target, backup_path=backup)

    # ABSENT
    _atomic_write(target, template)
    _log.info("beads_prime_install", action="installed", path=str(target))
    return InstallOutcome("installed", target)


def _describe_outcome(outcome: InstallOutcome) -> str:
    if outcome.action in ("installed", "updated"):
        msg = f"Beads PRIME.md: {outcome.action} ({outcome.path}). {UNDO_HINT}"
        if outcome.backup_path is not None:
            msg += f" Previous content backed up to {outcome.backup_path}."
        return msg
    if outcome.action == "up to date":
        return f"Beads PRIME.md: up to date ({outcome.path})"
    if outcome.action == "left alone (user-authored)":
        return (
            f"Beads PRIME.md: left alone -- user-authored (or hand-edited) "
            f"file at {outcome.path}"
        )
    if outcome.action == "left alone (installed is newer)":
        return (
            f"Beads PRIME.md: left alone -- {outcome.path} carries a newer "
            f"template version than this install ships"
        )
    return f"Beads PRIME.md: {outcome.action} ({outcome.path})"


def install_and_describe(*, disabled: bool = False) -> str | None:
    """CLI-facing convenience: honour the opt-outs, detect beads,
    install/refresh the user-level ``PRIME.md`` when applicable, and
    return one human-readable line describing what happened -- or
    ``None`` when beads was not detected at all (nothing to report).

    *disabled* is the caller's own ``--no-beads-prime`` flag value (an
    explicit per-invocation decline). The persisted config key
    (:func:`manage_enabled`) is consulted unconditionally beside it --
    either alone is sufficient to skip. Both outrank detection: a decline
    is reported even when beads was never going to be touched anyway, so
    the operator sees their flag was recognised.

    Callers (``nx init``, ``nx upgrade``) wrap this in their own
    best-effort ``try``/``except`` per their established convention
    (mirroring ``_seed_builtin_plans_best_effort`` et al.) -- this function
    itself does not swallow errors, so a caller's `except` block sees the
    real exception.
    """
    if disabled:
        return "Beads PRIME.md: skipped (--no-beads-prime)"
    if not manage_enabled():
        return "Beads PRIME.md: skipped (beads_prime.manage is set to false)"
    detected, _reason = beads_detected()
    if not detected:
        return None
    outcome = install()
    return _describe_outcome(outcome)
