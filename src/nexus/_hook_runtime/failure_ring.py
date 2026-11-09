# SPDX-License-Identifier: AGPL-3.0-or-later
"""A bounded local ring of the payloads behind swallowed hook crashes, and their replay (nexus-3ceda.3).

:func:`nexus._hook_runtime._io.never_fail` swallows a hook's crash by design: a
hook must never break the session. What it logged was the exception text, and
the payload that caused it was gone, so a crash that happens only on one shape
of input (one tool's ``tool_input``, a prompt with an odd character) could not
be reproduced. Now the boundary also writes the payload here, and
:func:`replay` (``nx hook replay <file>``) runs the same verb on it again with
the boundary off, so the traceback surfaces.

**Local and redacted, not anonymised.** A payload carries prompts, command
lines and file text, and those are kept: they are what a replay needs.
Credentials are not. Every string goes through :func:`scrub`, which masks
``Authorization``/``Bearer``/``Basic`` values, ``NAME_TOKEN=...``-style
assignments (quoted JSON keys included), URL userinfo, ``curl -u user:pass``
and well-known token prefixes, then :func:`nexus.redact.redact_credentials`.
A value under a credential-named key is dropped whole. The environment keeps
only ``NX_``/``NEXUS_``/``CLAUDE_`` names without a credential word
(:data:`_ENV_CREDENTIAL_WORDS`), values scrubbed. Entries stay under the
config directory (files 0600 in a 0700 directory on POSIX; on Windows the
profile's ACLs decide) and nothing sends them anywhere.
``NX_HOOK_FAILURE_RING=0`` turns the capture off.

**Bounded.** At most :data:`MAX_ENTRIES` entries and :data:`MAX_TOTAL_BYTES`
in all; the oldest go first, never the entry just written. One entry is at
most :data:`MAX_ENTRY_BYTES`: long strings and long lists are cut, and a
payload still too big is replaced by a note of its size; ``truncated`` says so.

**It never raises.** It runs inside a crash handler; a failure to record is
dropped. Module scope imports only the standard library, like every module in
this package (``tests/hooks/test_hook_runtime_thin.py``); only a crash pays for
the rest.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: The most entries the ring keeps.
MAX_ENTRIES: int = 50
#: The most bytes the ring's entries take in all.
MAX_TOTAL_BYTES: int = 2 * 1024 * 1024
#: The most bytes one entry takes.
MAX_ENTRY_BYTES: int = 128 * 1024
#: Entry format version, for a replay of an entry written by another release.
FORMAT: int = 1
#: Set to ``0`` (or false/no/off) to record nothing.
DISABLE_ENV: str = "NX_HOOK_FAILURE_RING"
#: The module roots a replay may import: the hook verbs. An entry is a file
#: someone may hand you; replaying it must not import whatever it names.
REPLAY_MODULE_ROOTS: tuple[str, ...] = ("nexus.hooks.", "nexus._hook_runtime.")

_PREFIX = "hookfail-"
_SUFFIX = ".json"
_TMP_SUFFIX = ".json.tmp"
#: Key tokens (a key split at ``_``, ``-``, ``.`` and camelCase) that mark a credential
#: value. Whole tokens, so ``author``, ``passed`` and ``compass`` are not credentials.
_CREDENTIAL_TOKENS = frozenset({"token", "password", "passwd", "pass", "pwd", "secret", "secrets",
                                "authorization", "auth", "apikey", "cookie", "cookies", "credential",
                                "credentials", "dsn", "jwt", "bearer", "key", "passphrase", "signature"})
_KEY_SPLIT = re.compile(r"[_\-.\s]+|(?<=[a-z0-9])(?=[A-Z])")
_ENV_PREFIXES = ("NX_", "NEXUS_", "CLAUDE_")
#: Words in a variable's name that keep it out of a replay even when it was recorded:
#: a replayed entry is a file someone may hand you, and these steer where code,
#: config or a connection comes from.
_ENV_NO_REPLAY_WORDS = ("PATH", "DIR", "HOME", "ROOT", "BIN", "CMD", "EXE", "PROG", "PLUGIN", "HOST",
                        "PORT", "ENDPOINT", "SERVICE", "PYTHON", "SHELL", "EDITOR", "HOOK")
_ENV_CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASS", "PWD", "AUTH", "CREDENTIAL", "COOKIE",
                         "DSN", "URL", "CONN", "PRIVATE", "SALT")
_LIST_KEEP = 200
_PRECUT_CHARS = 16 * 1024
_MASK = "[redacted]"
_TMP_STALE_S = 60.0

# Repeats are bounded: an unbounded name class backtracks quadratically on a
# long run of word characters, and this runs inside a hook's crash handler.
# A short credential word must stand as its own token (``auth`` in ``AUTH_TOKEN``, not in
# ``author``; ``pass`` in ``DB_PASS``, not in ``compass`` or ``passed``).
# The long words may be glued on (PGPASSWORD, GITHUBTOKEN); the short ones only as
# their own token.
_NAME = (r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,64}?"
         r"(?:passw(?:or)?d|passphrase|secret|credentials?|api[_-]?key|token(?![a-z])"
         r"|(?<![a-z])(?:pass|pwd|key|auth|cookie|dsn|jwt)(?![a-z]))"
         r"[A-Za-z0-9_.-]{0,64}")
_SCRUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <tok>, Authorization: Basic <tok>, and a bare "Bearer <tok>"
    # whose token has a digit or a symbol, so "the basic structure" stays prose.
    (re.compile(r"(?i)\b((?:authorization|proxy-authorization)[\"']?\s*[:=]\s*[\"']?)"
                r"((?:bearer|basic|token|digest)\s+)?[^\s\"']+"),
     rf"\1\2{_MASK}"),
    # camelCase credential keys in serialized text ("sessionKey": "...", privateKey=...),
    # case-sensitive so a plain word ending in "key" is not one.
    (re.compile(r"(?<![A-Za-z0-9_])(\"?[a-z][A-Za-z0-9]{0,64}?(?:Key|Token|Secret|Password|Passwd|Credential)s?\"?)"
                r"(\s*[:=]\s*)(\"[^\"\n]{0,512}\"|'[^'\n]{0,512}'|[^\s\"',;&|)}]+)"), rf"\1\2{_MASK}"),
    # A Cookie or Set-Cookie header: every pair on the line.
    (re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]{1,4096}"), rf"\1{_MASK}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+(?=[A-Za-z]{0,64}[0-9._~+/=-])[A-Za-z0-9._~+/=-]{6,}"), rf"\1 {_MASK}"),
    # NAME=value, NAME: value, "name": "value", --name=value, --password value, with a
    # credential word in NAME; a quoted value is masked whole, spaces included.
    (re.compile(rf"(?i)({_NAME})([\"']?\s*[:=]\s*|\s+(?=[\"']))(\"[^\"\n]{{0,512}}\"|'[^'\n]{{0,512}}'|[^\s\"',;&|)]+)"),
     rf"\1\2{_MASK}"),
    (re.compile(r"(?<!\S)(--(?:password|passwd|pass|token|secret|api-key))(\s+)([^\s-]\S*)"), rf"\1\2{_MASK}"),
    # URL userinfo: scheme://user:pw@host
    (re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,15}://[^/\s:@]{1,256}):[^@\s/]{1,256}@"), rf"\1:{_MASK}@"),
    # curl -u user:pass / --user user:pass
    (re.compile(r"(?<!\S)(-u|--user)(\s+|=)([^\s:]+):\S+"), rf"\1\2\3:{_MASK}"),
    # Well-known token shapes.
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[opsur]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|xox[abposr]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|pa-[A-Za-z0-9_-]{20,})"), _MASK),
)


def disabled(env: Mapping[str, str] | None = None) -> bool:
    raw = ((os.environ if env is None else env).get(DISABLE_ENV) or "").strip().lower()
    return raw in ("0", "false", "no", "off")


def ring_dir(env: Mapping[str, str] | None = None) -> Path:
    """``<config dir>/hook-failures``, the config dir as :mod:`nexus.logging_setup` finds it."""
    env = os.environ if env is None else env
    base = env.get("NEXUS_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".config" / "nexus") / "hook-failures"


_PEM_BEGIN = "-----BEGIN "
_PEM_KEY = "PRIVATE KEY-----"
_PEM_END = "-----END "


def _mask_pem(text: str) -> str:
    """Every PEM private key block masked whole. A plain scan, not a regex: a lazy
    span between BEGIN and END backtracks quadratically on BEGIN markers with no END."""
    if _PEM_KEY not in text:
        return text
    out, i = [], 0
    while (b := text.find(_PEM_BEGIN, i)) >= 0:
        head_end = text.find(_PEM_KEY, b, b + 80)
        if head_end < 0:
            out.append(text[i:b + len(_PEM_BEGIN)])
            i = b + len(_PEM_BEGIN)
            continue
        e = text.find(_PEM_END, head_end)
        tail = text.find(_PEM_KEY, e) if e >= 0 else -1
        if tail < 0:
            out.append(text[i:b] + f"-----BEGIN PRIVATE KEY-----{_MASK} (no END line)")
            return "".join(out)
        out.append(text[i:b] + f"-----BEGIN PRIVATE KEY-----{_MASK}-----END PRIVATE KEY-----")
        i = tail + len(_PEM_KEY)
    out.append(text[i:])
    return "".join(out)


def scrub(text: str) -> str:
    """*text* with credentials masked: PEM keys, the ring's own patterns, then
    :func:`nexus.redact.redact_credentials`."""
    from nexus.redact import redact_credentials  # noqa: PLC0415 -- crash path only

    text = _mask_pem(text)
    for pattern, repl in _SCRUBS:
        text = pattern.sub(repl, text)
    return redact_credentials(text)


def _credential_key(key: str) -> bool:
    tokens = [t.lower() for t in _KEY_SPLIT.split(key[:256]) if t]
    joined = "".join(tokens)
    return any(t in _CREDENTIAL_TOKENS for t in tokens) or joined in ("apikey", "privatekey", "accesskey")


def _plain_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float))


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Mapping):
        # A credential-named key masks a string or a structure whole; a bool, number or
        # None under it is kept, so a replay still gets the type the verb expects.
        return {scrub(str(k)): (_MASK if _credential_key(str(k)) and not _plain_scalar(v) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(repr(value))


def _env_recordable(name: str) -> bool:
    return name.startswith(_ENV_PREFIXES) and not any(w in name.upper() for w in _ENV_CREDENTIAL_WORDS)


def _env_replayable(name: str) -> bool:
    return _env_recordable(name) and not any(w in name.upper() for w in _ENV_NO_REPLAY_WORDS)


def _env_subset(env: Mapping[str, str]) -> dict[str, str]:
    return dict(sorted((name, scrub(value)) for name, value in env.items() if _env_recordable(name)))


def _encode(entry: Mapping[str, Any]) -> bytes:
    """The bytes written for *entry*: what the cap is measured on."""
    return json.dumps(entry, ensure_ascii=False).encode("utf-8")


def _cut(value: Any, cap: int) -> Any:
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + f"...[cut {len(value) - cap} chars]"
    if isinstance(value, Mapping):
        items = list(value.items())
        out = {k: _cut(v, cap) for k, v in items[:_LIST_KEEP]}
        if len(items) > _LIST_KEEP:
            out["...[cut]"] = f"{len(items) - _LIST_KEEP} keys"
        return out
    if isinstance(value, (list, tuple)):
        value = list(value)
        head = [_cut(v, cap) for v in value[:_LIST_KEEP]]
        return head + ([f"...[cut {len(value) - _LIST_KEEP} items]"] if len(value) > _LIST_KEEP else [])
    return value


#: Characters the pre-scrub cut keeps across a whole payload (keys and values),
#: so the scrub's cost is bounded however the payload is shaped.
_WALK_BUDGET_CHARS = 256 * 1024
_MAX_DEPTH = 32


def _precut(value: Any, budget: list[int], depth: int = 0) -> Any:
    """*value* cut before scrubbing: strings to :data:`_PRECUT_CHARS`, keys to 256
    characters, containers to :data:`_LIST_KEEP` items, nesting to 32, and the
    whole walk to :data:`_WALK_BUDGET_CHARS`."""
    if budget[0] <= 0 or depth > _MAX_DEPTH:
        return "...[cut: over the size budget]"
    budget[0] -= 1  # every node costs something, so a wide tree of numbers is bounded too
    if isinstance(value, str):
        kept = value[:min(_PRECUT_CHARS, budget[0])]
        budget[0] -= len(kept)
        return kept + (f"...[cut {len(value) - len(kept)} chars]" if len(kept) < len(value) else "")
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for n, (k, v) in enumerate(value.items()):
            if n >= _LIST_KEEP or budget[0] <= 0:
                out["...[cut]"] = f"{len(value) - n} keys"
                break
            key = str(k)[:256]
            budget[0] -= len(key)
            out[key] = _precut(v, budget, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for n, v in enumerate(value):
            if n >= _LIST_KEEP or budget[0] <= 0:
                items.append(f"...[cut {len(value) - n} items]")
                break
            items.append(_precut(v, budget, depth + 1))
        return items
    return value


def _cut_to_fit(entry: dict[str, Any], limit: int) -> dict[str, Any]:
    """Cut long strings and lists until the entry fits; failing that, replace the
    payload, then the traceback, by a note of its size. Records ``truncated``."""
    cap = 16 * 1024
    while len(_encode(entry)) > limit and cap >= 64:
        entry = {k: (_cut(v, cap) if k in ("payload", "traceback", "error", "env") else v) for k, v in entry.items()}
        entry["truncated"] = True
        cap //= 2
    for field in ("payload", "traceback", "env"):
        if len(_encode(entry)) <= limit:
            break
        entry[field] = f"[dropped: {len(_encode({field: entry[field]}))} bytes after cutting]"
        entry["truncated"] = True
    return entry


def entries(directory: Path | None = None) -> list[Path]:
    """The ring's entry files, oldest first (the names sort by time)."""
    directory = ring_dir() if directory is None else directory
    try:
        return sorted(p for p in directory.iterdir()
                      if p.name.startswith(_PREFIX) and p.name.endswith(_SUFFIX) and p.is_file())
    except OSError:
        return []


def prune(directory: Path, *, max_entries: int | None = None, max_bytes: int | None = None,
          keep: Path | None = None) -> int:
    """Delete the oldest entries until the ring fits both caps (default
    :data:`MAX_ENTRIES` and :data:`MAX_TOTAL_BYTES`); return how many went.
    *keep* (the entry just written) is never deleted. Only this ring's own
    files (``hookfail-*.json``, and leftover ``hookfail-*.json.tmp``) are touched."""
    max_entries = MAX_ENTRIES if max_entries is None else max_entries
    max_bytes = MAX_TOTAL_BYTES if max_bytes is None else max_bytes
    try:
        import time  # noqa: PLC0415 -- crash and CLI paths only

        cutoff = time.time() - _TMP_STALE_S
        for p in directory.iterdir():
            # Only a temp file old enough to be abandoned: a fresh one is another
            # process's write in flight.
            if p.name.startswith(_PREFIX) and p.name.endswith(_TMP_SUFFIX) and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass
    found = entries(directory)
    sizes = []
    for p in found:
        try:
            sizes.append(p.stat().st_size)
        except OSError:
            sizes.append(0)
    total, left, removed = sum(sizes), len(found), 0
    for p, size in zip(found, sizes):
        if left <= max_entries and total <= max_bytes:
            break
        if keep is not None and p == keep:
            continue
        try:
            p.unlink()
            removed += 1
        except FileNotFoundError:
            pass  # another process pruned it: it is gone all the same
        except OSError:
            continue
        total -= size
        left -= 1
    return removed


def record(
    hook: str,
    payload: Any,
    exc: BaseException,
    *,
    module: str | None,
    env: Mapping[str, str] | None = None,
    directory: Path | None = None,
    now: float | None = None,
) -> Path | None:
    """Write one entry for a swallowed crash of *hook* on *payload*; return its path,
    or None when capture is off, there is no *module* to replay, or anything at all
    goes wrong. *module* is the dotted path of the module whose ``run(payload)``
    raised, which :func:`replay` calls."""
    tmp: Path | None = None
    try:
        env = os.environ if env is None else env
        if disabled(env) or not module:
            return None
        import datetime as _dt  # noqa: PLC0415 -- crash path only
        import time  # noqa: PLC0415 -- crash path only
        import traceback  # noqa: PLC0415 -- crash path only

        # Owner-only on Windows too: entries can hold prompts and command lines.
        from nexus._winsec import make_user_dir, open_private  # noqa: PLC0415 -- crash path only

        directory = ring_dir(env) if directory is None else directory
        make_user_dir(directory)
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
            # Cut before scrubbing, like the payload: an exception message can embed a whole tool response.
            "error": scrub(_cut(str(exc), _PRECUT_CHARS)),
            "traceback": scrub(_cut("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                                    _PRECUT_CHARS)),
            # Cut before scrubbing, so the scrub's cost is bounded by the entry
            # cap, not by the payload: a crash on a 50 MB tool response must
            # not turn the hook's crash handler into a hang.
            "payload": _redact(precut := _precut(payload, [_WALK_BUDGET_CHARS])),
            "env": _env_subset(env),
        }
        try:
            if precut != payload:
                entry["truncated"] = True
        except Exception:  # noqa: BLE001 -- an odd payload type must not cost the entry
            pass
        data = _encode(_cut_to_fit(entry, MAX_ENTRY_BYTES))
        safe_hook = "".join(c if c.isalnum() or c in "-_" else "_" for c in hook)[:40]
        name = f"{_PREFIX}{when.strftime('%Y%m%dT%H%M%S')}{int(stamp * 1e6) % 1_000_000:06d}-{os.getpid()}-{safe_hook}"
        path = directory / (name + _SUFFIX)
        tmp = directory / (name + _TMP_SUFFIX)
        fd = open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        tmp = None
        prune(directory, keep=path)
        return path if path.exists() else None
    except Exception:  # noqa: BLE001 -- runs inside a crash handler; recording is best-effort
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def load(path: Path) -> dict[str, Any]:
    """Read one entry, refusing a file that is not one."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "hook" not in data or "payload" not in data:
        raise ValueError(f"{path} is not a hook-failure entry")
    if data.get("format") != FORMAT:
        raise ValueError(f"{path} has entry format {data.get('format')!r}; this release reads {FORMAT}")
    return data


class ReplayRefused(ValueError):
    """The entry cannot be replayed: it names no module, or one that is not a hook verb."""


@contextmanager
def _recorded_context(entry: Mapping[str, Any]) -> Generator[None]:
    """The recorded environment subset and working directory, for the length of a replay."""
    raw = entry.get("env")
    # Only what record() itself would keep, and nothing that steers where code,
    # config or a connection comes from (PATH, PYTHONPATH, a config dir, a host).
    env: dict[str, Any] = ({k: v for k, v in raw.items() if isinstance(k, str) and _env_replayable(k)}
                           if isinstance(raw, dict) else {})
    saved = {k: os.environ.get(k) for k in env}
    cwd = os.getcwd()
    try:
        for k, v in env.items():
            if isinstance(v, str):
                os.environ[k] = v
        target = entry.get("cwd")
        if isinstance(target, str) and os.path.isdir(target):
            os.chdir(target)
        yield
    finally:
        os.chdir(cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def replay(path: Path, *, module_roots: Sequence[str] | None = None, recorded_env: bool = False) -> Any:
    """Run the recorded hook's ``run(payload)`` again, OUTSIDE the never-fail boundary,
    so the crash it recorded raises here with its traceback. With *recorded_env*,
    also under the recorded environment subset (less anything naming a path, dir,
    host or interpreter) and working directory: only for an entry you recorded
    yourself, since a handed-over entry's flags and directory steer what runs. Returns the verb's
    :class:`~nexus._hook_runtime._io.HookResult` if it no longer crashes.

    Refuses a module outside *module_roots* (the hook verbs). This runs the verb
    for real, side effects included (a mailbox drain consumes rows; an upgrade
    upgrades). The payload is the redacted one, and a ``truncated`` entry was cut,
    so a crash that depended on what was masked or cut may not come back."""
    import importlib  # noqa: PLC0415

    entry = load(path)
    module = entry.get("module")
    if not isinstance(module, str) or not module:
        raise ReplayRefused(f"{path} names no module to replay (hook {entry['hook']!r})")
    if not module.startswith(tuple(REPLAY_MODULE_ROOTS if module_roots is None else module_roots)):
        raise ReplayRefused(f"{path} names module {module!r}, which is not a hook verb; replay refuses it")
    payload = entry["payload"]
    if not recorded_env:
        return importlib.import_module(module).run(payload if isinstance(payload, dict) else None)
    with _recorded_context(entry):
        return importlib.import_module(module).run(payload if isinstance(payload, dict) else None)


__all__ = ["DISABLE_ENV", "MAX_ENTRIES", "MAX_ENTRY_BYTES", "MAX_TOTAL_BYTES", "REPLAY_MODULE_ROOTS",
           "ReplayRefused", "disabled", "entries", "load", "prune", "record", "replay", "ring_dir", "scrub"]
