# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deny rules that keep a dispatched ``claude -p`` child away from sensitive documents' source files
(RDR-243 Phase 3, fix round r1, critic C1).

Sensitivity governs nexus reads, not the filesystem: a child that nexus dispatches with Read, Grep or Glob
(``nx_enrich_beads``, ``nx_plan_audit``) could open a sensitive document's source file from its working tree
without passing through any filter. :func:`file_tool_restriction` reads the catalog's sensitive documents and
returns what the dispatch must put on the child's command line.

**Rule syntax and source.** Claude Code permission rules, passed as ``--disallowedTools`` (one argv element per
rule). A Read deny rule covers the Read, Grep and Glob built-ins (docs: "Read rules apply to all built-in tools
that read files like Grep and Glob", a best-effort statement; ``Grep(path)`` and ``Glob(path)`` are not valid
path rules and draw a startup warning). Path form: ``Read(//abs/path)`` for an absolute path (a single leading
slash anchors at the settings source, not the filesystem root), ``Read(./rel)`` relative to the child's working
directory, ``Read(//dir/**)`` for a whole directory; gitignore syntax, so ``\\ * ? [ ]`` are escaped.
On Windows Claude Code normalises ``C:\\Users\\alice`` to ``/c/Users/alice`` before matching and documents
``Read(//c/Users/**)`` for it, so a drive-letter location (``file:///C:/...``, ``C:\\...``) is written that way
(:func:`_posix_drive_path`); the text after the drive letter keeps its case, which the docs do not address.
A drive path is rewritten from its Windows spelling, and its real path (a symlink, a junction, an 8.3 short name)
is resolved through the Windows filesystem before it is written the same way. On Windows the catalog's raw
``"file://" + C:\\Users\\a\\x.md`` registration spelling (two slashes, a backslash path) is a file location like
``file:///C:/Users/a/x.md``; both give ``Read(//c/Users/a/x.md)``.
Sources: https://code.claude.com/docs/en/permissions (Read and Edit), /cli-reference (--disallowedTools).

**Bound.** At most :data:`MAX_DENY_RULES` (100) rules. Over that, one rule per parent directory of the
sensitive files (a directory deny can only over-deny). Over that in directories as well, or when a path cannot
be written as a rule, the file tools are taken away from the child instead (``drop_file_tools``).

**Fail closed.** A ``file:`` location or a bare path that cannot be read as a local path (a UNC share, a
``file://host/...`` authority, a drive path whose real path leaves the drives) gives ``drop_file_tools``; a
sensitive document never produces "no rule" because its path was not understood. A catalog that cannot be listed gives ``drop_file_tools``: the dispatch removes Read, Grep and
Glob from the child by name. Nothing is cached: the flag is one-way and can be set between two dispatches.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

__all__ = ["FILE_TOOLS", "MAX_DENY_RULES", "FileToolRestriction", "file_tool_restriction", "grants_file_tools"]

#: The built-ins that read file content. A Read deny rule covers all three.
FILE_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

#: Bound on the deny rules one dispatch carries (argv size and Claude Code's rule parser are the limits).
MAX_DENY_RULES: int = 100

#: Characters a rule cannot carry safely: the tool-list parser splits on commas and a newline ends a rule.
_UNSAFE = frozenset(",\n\r\t\0")
_GITIGNORE_SPECIAL = "\\*?[]"


@dataclass(frozen=True)
class FileToolRestriction:
    """What a dispatch does about the file tools. The default restricts nothing."""

    deny_rules: tuple[str, ...] = ()
    #: Fail closed: remove Read, Grep and Glob from the child (the catalog or the bound could not give rules).
    drop_file_tools: bool = False
    reason: str = ""
    #: Catalog documents listed, and how many of them were sensitive (the non-vacuity evidence).
    examined: int = 0
    sensitive: int = 0


def grants_file_tools(allowed_tools: list[str] | None) -> bool:
    """True when *allowed_tools* names Read, Grep or Glob (bare or with a path specifier)."""
    return any(t.split("(", 1)[0].strip() in FILE_TOOLS for t in allowed_tools or ())


def _escape(path: str) -> str:
    return "".join("\\" + c if c in _GITIGNORE_SPECIAL else c for c in path)


def _is_safe(path: str) -> bool:
    return not any(c in _UNSAFE or ord(c) < 32 for c in path)


_DRIVE = re.compile(r"^/?([A-Za-z]):(?=[\\/]|$)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


class _Unrecognisable(Exception):
    """A location that is, or may be, a local file but cannot be written as a deny rule."""


def _posix_drive_path(path: str) -> str:
    """``/C:/Users/a`` or ``C:\\Users\\a`` as ``/c/Users/a``, the form Claude Code matches on Windows; ``""`` when
    *path* carries no drive letter."""
    m = _DRIVE.match(path)
    if m is None:
        return ""
    return "/" + m.group(1).lower() + path[m.end():].replace("\\", "/")


def _drive_locator(path: str) -> str:
    """A drive path as ``C:/Users/a`` (forward slashes, the drive letter as written), or ``""``."""
    m = _DRIVE.match(path)
    return "" if m is None else m.group(1) + ":" + path[m.end():].replace("\\", "/")


def _locator(entry: Any) -> str:
    """The local path an entry's source lives at, as ``/abs/path``, ``C:/abs/path`` or ``./rel/path``; ``""`` for
    none (the entry is not a local file). Raises :class:`_Unrecognisable` for a ``file:`` location or a path whose
    form this module cannot write as a rule.

    ``file://`` + path source URIs are taken verbatim after the scheme, as ``search_engine._local_source_path``
    does: every registration path mints the raw ``"file://" + absolute``, so on Windows the spelling is
    ``file://C:\\Users\\a\\x.md`` (two slashes, backslashes) as well as ``file:///C:/Users/a/x.md``. Any other
    scheme is not a file and is skipped. An entry with no URI falls back to its ``file_path``."""
    uri = getattr(entry, "source_uri", "") or ""
    if isinstance(uri, str) and uri:
        if drive := _drive_locator(uri):  # a bare drive path in the URI field
            return drive
        if not uri[:5].lower() == "file:":
            return "" if _SCHEME.match(uri) else _bare(uri)
        if not uri[:7].lower() == "file://":
            raise _Unrecognisable(f"file location {uri[:80]!r}")
        rest = uri[len("file://"):]
        if drive := _drive_locator(rest):
            return drive
        if rest.startswith("/") and not rest.startswith("//"):
            return rest
        raise _Unrecognisable(f"file location {uri[:80]!r}")
    fp = getattr(entry, "file_path", "") or ""
    if not isinstance(fp, str) or not fp or "://" in fp:
        return ""
    return _bare(fp)


def _bare(fp: str) -> str:
    if drive := _drive_locator(fp):
        return drive
    if fp.startswith("\\\\") or fp.startswith("//"):
        raise _Unrecognisable(f"network path {fp[:80]!r}")
    if os.name == "nt":
        fp = fp.replace("\\", "/")
    if fp.startswith("/"):
        return fp
    return "./" + fp[2:] if fp.startswith("./") else "./" + fp


def _real_drive_path(native: str) -> str:
    """*native* resolved through the Windows filesystem as ``C:/...``; raises when the real path is no drive path."""
    real = os.path.realpath(native)
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if real.startswith(prefix):
            real = real[len(prefix):]
    if drive := _drive_locator(real):
        return drive
    raise _Unrecognisable(f"real path {real[:80]!r}")


def _variants(locator: str) -> list[str]:
    """The rule-form spellings of *locator*: itself plus, for an absolute path whose real path differs (a symlink),
    the real path. A drive path is returned as ``/c/...``."""
    drive = _DRIVE.match(locator) is not None
    out = [_posix_drive_path(locator) if drive else locator]
    if not locator.startswith("./") and os.path.lexists(locator) and (drive == (os.name == "nt")):
        # an absent path has no real path (macOS firmlinks rewrite one); a drive path exists only on Windows, a
        # rooted POSIX one only off it
        try:
            real = _real_drive_path(locator) if drive else os.path.realpath(locator)
        except OSError:
            real = locator
        real = _posix_drive_path(real) if drive else real
        if real != out[0]:
            out.append(real)
    return out


def _safe_ancestor(path: str) -> str:
    """The nearest ancestor directory of *path* whose spelling is safe, or ``""`` when only ``/`` is left."""
    parent = os.path.dirname(path)
    while parent not in ("", "/", "."):
        if _is_safe(parent):
            return parent
        parent = os.path.dirname(parent)
    return ""


def _file_rule(path: str) -> str:
    return f"Read(/{_escape(path)})" if path.startswith("/") else f"Read({_escape(path)})"


def _dir_rule(directory: str) -> str:
    return f"Read(/{_escape(directory)}/**)" if directory.startswith("/") else f"Read({_escape(directory)}/**)"


def _restrict(entries: list[Any]) -> FileToolRestriction:
    by_tumbler = {str(getattr(e, "tumbler", "")): e for e in entries}
    flagged = {t for t, e in by_tumbler.items() if getattr(e, "sensitive", False) is True}
    # A merged duplicate points at the canonical (alias_of); the flag lives on the canonical, and the
    # duplicate's file holds the same content.
    chosen = [e for e in entries if getattr(e, "sensitive", False) is True or str(getattr(e, "alias_of", "") or "") in flagged]
    if not chosen:
        return FileToolRestriction(examined=len(entries), sensitive=0)

    files: dict[str, None] = {}  # ordered, deduped
    safe_dirs: dict[str, None] = {}
    for e in chosen:
        try:
            loc = _locator(e)
            variants = _variants(loc) if loc else []
        except _Unrecognisable as exc:
            return FileToolRestriction(
                drop_file_tools=True, examined=len(entries), sensitive=len(chosen),
                reason=f"a sensitive document's location cannot be written as a deny rule: {exc}"[:300],
            )
        for v in variants:
            if _is_safe(v):
                files[v] = None
            else:
                anc = _safe_ancestor(v) if v.startswith("/") else ""
                if not anc:
                    return FileToolRestriction(
                        drop_file_tools=True, examined=len(entries), sensitive=len(chosen),
                        reason=f"a sensitive document's path cannot be written as a deny rule: {v[:80]!r}",
                    )
                safe_dirs[anc] = None
    rules = [_file_rule(p) for p in files] + [_dir_rule(d) for d in safe_dirs]
    if len(rules) > MAX_DENY_RULES:
        dirs = dict.fromkeys([os.path.dirname(p) for p in files] + list(safe_dirs))
        if dirs.keys() & {"", "/", "."}:
            return FileToolRestriction(
                drop_file_tools=True, examined=len(entries), sensitive=len(chosen),
                reason="sensitive files sit directly under the filesystem root or the working directory",
            )
        rules = [_dir_rule(d) for d in dirs]
        if len(rules) > MAX_DENY_RULES:
            return FileToolRestriction(
                drop_file_tools=True, examined=len(entries), sensitive=len(chosen),
                reason=f"{len(chosen)} sensitive documents need more than {MAX_DENY_RULES} deny rules, "
                       f"even by directory ({len(rules)})",
            )
    return FileToolRestriction(deny_rules=tuple(rules), examined=len(entries), sensitive=len(chosen))


def _default_reader() -> Any:
    """The factory's catalog read handle (a seam: the unit suite stands in "no sensitive documents" here)."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: heavy import graph

    return make_catalog_reader()


def file_tool_restriction(*, reader: Any = None) -> FileToolRestriction:
    """What to do about Read, Grep and Glob for one dispatch, from the catalog's live sensitive documents.

    Blocking (the catalog is read over HTTP): call it from a thread. *reader* is the catalog read handle; with
    none, the factory's. Any failure to list the catalog is ``drop_file_tools`` (never "no rules")."""
    try:
        if reader is None:
            reader = _default_reader()
        if reader is None:
            raise RuntimeError("no catalog reader")
        entries = list(reader.all_documents(limit=0))
    except Exception as exc:  # noqa: BLE001 — every failure of the read is "unknown", whatever its class
        _log.warning("dispatch_file_deny_catalog_unreadable", error=str(exc)[:300])
        return FileToolRestriction(
            drop_file_tools=True, reason=f"the catalog could not be listed ({type(exc).__name__}: {exc})"[:300],
        )
    result = _restrict(entries)
    _log.info(
        "dispatch_file_deny", examined=result.examined, sensitive=result.sensitive,
        rules=len(result.deny_rules), dropped=result.drop_file_tools,
    )
    return result
