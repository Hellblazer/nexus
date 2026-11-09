# SPDX-License-Identifier: AGPL-3.0-or-later
"""A bounded local ring of the payloads behind swallowed hook crashes, and their replay (nexus-3ceda.3).

:func:`nexus._hook_runtime._io.never_fail` swallows a hook's crash by design: a
hook must never break the session. What it logged was the exception text, and
the payload that caused it was gone, so a crash that happens only on one shape
of input (one tool's ``tool_input``, a prompt with an odd character) could not
be reproduced. Now the boundary also writes the payload here, and
:func:`replay` (``nx hook replay <file>``) runs the same verb on it again with
the boundary off, so the traceback surfaces.

**Local and redacted.** A payload can carry a prompt, a command line or file
text. Entries stay under the config directory (files 0600 in a 0700
directory), nothing sends them anywhere, every string goes through
:func:`nexus.redact.redact_credentials`, a value under a credential-named key
(``token``, ``password``, ``authorization`` ...) is dropped, and the
environment is an allowlist of nexus and Claude Code variables with credential
names removed. ``NX_HOOK_FAILURE_RING=0`` turns the capture off.

**Bounded.** At most :data:`MAX_ENTRIES` entries and :data:`MAX_TOTAL_BYTES`
in all; the oldest go first. One entry is at most :data:`MAX_ENTRY_BYTES`,
long strings cut to fit, with the cut recorded.

**It never raises.** It runs inside a crash handler; a failure to record is
dropped. Module scope imports only the standard library, like every module in
this package (``tests/hooks/test_hook_runtime_thin.py``); only a crash pays for
the rest.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: The most entries the ring keeps.
MAX_ENTRIES: int = 50
#: The most bytes the ring's entries take in all.
MAX_TOTAL_BYTES: int = 2 * 1024 * 1024
#: The most bytes one entry takes; longer strings in it are cut.
MAX_ENTRY_BYTES: int = 128 * 1024
#: Entry format version, for a replay of an entry written by another release.
FORMAT: int = 1
#: Set to ``0`` to record nothing.
DISABLE_ENV: str = "NX_HOOK_FAILURE_RING"

_PREFIX = "hookfail-"
_SUFFIX = ".json"
_CREDENTIAL_WORDS = ("token", "password", "passwd", "secret", "authorization", "api_key", "apikey",
                     "cookie", "credential", "private_key")
_ENV_PREFIXES = ("NX_", "NEXUS_", "CLAUDE_")
_ENV_NAMES = frozenset({"VIRTUAL_ENV", "PYTHONPATH"})
_ENV_CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL", "COOKIE")


def ring_dir(env: Mapping[str, str] | None = None) -> Path:
    """``<config dir>/hook-failures``, the config dir as :mod:`nexus.logging_setup` finds it."""
    env = os.environ if env is None else env
    base = env.get("NEXUS_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".config" / "nexus") / "hook-failures"


def _credential_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(word in k for word in _CREDENTIAL_WORDS)


def _redact(value: Any, scrub: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Mapping):
        return {str(k): "[redacted]" if _credential_key(str(k)) else _redact(v, scrub)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, scrub) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(repr(value))


def _env_subset(env: Mapping[str, str], scrub: Callable[[str], str]) -> dict[str, str]:
    keep = {}
    for name, value in env.items():
        if not (name.startswith(_ENV_PREFIXES) or name in _ENV_NAMES):
            continue
        if any(word in name.upper() for word in _ENV_CREDENTIAL_WORDS):
            continue
        keep[name] = scrub(value)
    return dict(sorted(keep.items()))


def _cut_to_fit(entry: dict[str, Any], limit: int) -> dict[str, Any]:
    """Halve the longest strings until the entry fits; record that it was cut."""
    cap = 16 * 1024
    while len(json.dumps(entry).encode("utf-8")) > limit and cap >= 64:
        def cut(v: Any) -> Any:
            if isinstance(v, str) and len(v) > cap:
                return v[:cap] + f"...[cut {len(v) - cap} chars]"
            if isinstance(v, dict):
                return {k: cut(x) for k, x in v.items()}
            if isinstance(v, list):
                return [cut(x) for x in v]
            return v

        entry = {k: (cut(v) if k in ("payload", "traceback", "error", "env") else v) for k, v in entry.items()}
        entry["truncated"] = True
        cap //= 2
    return entry


def entries(directory: Path | None = None) -> list[Path]:
    """The ring's entry files, oldest first (the names sort by time)."""
    directory = ring_dir() if directory is None else directory
    try:
        return sorted(p for p in directory.iterdir()
                      if p.name.startswith(_PREFIX) and p.name.endswith(_SUFFIX) and p.is_file())
    except OSError:
        return []


def prune(directory: Path, *, max_entries: int | None = None, max_bytes: int | None = None) -> int:
    """Delete the oldest entries until the ring fits both caps (default
    :data:`MAX_ENTRIES` and :data:`MAX_TOTAL_BYTES`); return how many went.
    Only this ring's own files (``hookfail-*.json``) are ever touched."""
    max_entries = MAX_ENTRIES if max_entries is None else max_entries
    max_bytes = MAX_TOTAL_BYTES if max_bytes is None else max_bytes
    found = entries(directory)
    sizes = []
    for p in found:
        try:
            sizes.append(p.stat().st_size)
        except OSError:
            sizes.append(0)
    total, removed = sum(sizes), 0
    for p, size in zip(found, sizes):
        if len(found) - removed <= max_entries and total <= max_bytes:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


def record(
    hook: str,
    payload: Any,
    exc: BaseException,
    *,
    module: str | None = None,
    env: Mapping[str, str] | None = None,
    directory: Path | None = None,
    now: float | None = None,
) -> Path | None:
    """Write one entry for a swallowed crash of *hook* on *payload*; return its path,
    or None when capture is off or anything at all goes wrong. *module* is the dotted
    path of the module whose ``run(payload)`` raised, which :func:`replay` calls."""
    try:
        env = os.environ if env is None else env
        if env.get(DISABLE_ENV, "").strip() == "0":
            return None
        import datetime as _dt  # noqa: PLC0415 -- crash path only
        import time  # noqa: PLC0415 -- crash path only
        import traceback  # noqa: PLC0415 -- crash path only

        try:
            from nexus.redact import redact_credentials as scrub  # noqa: PLC0415 -- crash path only
        except Exception:  # noqa: BLE001 -- without the redactor, record nothing rather than unredacted text
            return None
        directory = ring_dir(env) if directory is None else directory
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        stamp = time.time() if now is None else now
        when = _dt.datetime.fromtimestamp(stamp, _dt.timezone.utc)
        entry: dict[str, Any] = {
            "format": FORMAT,
            "hook": hook,
            "module": module,
            "recorded_at": when.isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "cwd": scrub(os.getcwd()),
            "error_type": type(exc).__name__,
            "error": scrub(str(exc)),
            "traceback": scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
            "payload": _redact(payload, scrub),
            "env": _env_subset(env, scrub),
        }
        entry = _cut_to_fit(entry, MAX_ENTRY_BYTES)
        safe_hook = "".join(c if c.isalnum() or c in "-_" else "_" for c in hook)[:40]
        name = f"{_PREFIX}{when.strftime('%Y%m%dT%H%M%S')}{int(stamp * 1e6) % 1_000_000:06d}-{os.getpid()}-{safe_hook}{_SUFFIX}"
        path = directory / name
        tmp = directory / (name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=1, sort_keys=False)
        os.replace(tmp, path)
        prune(directory)
        return path
    except Exception:  # noqa: BLE001 -- runs inside a crash handler; recording is best-effort
        return None


def load(path: Path) -> dict[str, Any]:
    """Read one entry, refusing a file that is not one."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "hook" not in data or "payload" not in data:
        raise ValueError(f"{path} is not a hook-failure entry")
    if data.get("format") != FORMAT:
        raise ValueError(f"{path} has entry format {data.get('format')!r}; this release reads {FORMAT}")
    return data


def replay(path: Path) -> Any:
    """Run the recorded hook's ``run(payload)`` again, OUTSIDE the never-fail boundary,
    so the crash it recorded raises here with its traceback. Returns the verb's
    :class:`~nexus._hook_runtime._io.HookResult` if it no longer crashes.

    This runs the verb for real, side effects included (a mailbox drain consumes
    rows; an upgrade upgrades). The payload is the redacted one, so a crash that
    depended on a redacted value may not come back."""
    import importlib  # noqa: PLC0415

    entry = load(path)
    module = entry.get("module")
    if not module:
        raise ValueError(f"{path} names no module to replay (hook {entry['hook']!r})")
    payload = entry["payload"]
    return importlib.import_module(module).run(payload if isinstance(payload, dict) else None)


__all__ = ["DISABLE_ENV", "MAX_ENTRIES", "MAX_ENTRY_BYTES", "MAX_TOTAL_BYTES", "entries", "load",
           "prune", "record", "replay", "ring_dir"]
