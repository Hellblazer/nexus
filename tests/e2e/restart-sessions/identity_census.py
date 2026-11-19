# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Proof that the restart sandbox bound no RDR-253 identity on the operator's endpoint (found 2026-11-19).

run.py loads this file by path (``_load_slash("identity_census")``), as it loads sandbox_work.py: it runs under ``python -I``,
which leaves its own directory off sys.path. The sandbox's sessions carry a copy of the operator's engine credential, and
before ``NX_IDENTITY_BIND=0`` / ``NX_IDENTITY_LEASE=0`` went into their environment each one bound an identity in the
operator's tenant (twelve ``rs_*`` rows). The switches are the fix; three checks here keep them honest. Every read is
``nx identity show ...`` in the operator's own environment and read only; none can change anything on the endpoint.

1. :func:`bind_switch_gap`, before any session starts: the INSTALLED nx (the sessions run it, not this checkout) must
   carry the hook's ``NX_IDENTITY_BIND`` switch. An nx that predates it ignores the variable, so the sandbox would bind;
   the run then ends unverified with no session started. A capability check on the installed code, no version number.
2. :func:`session_findings`, after cleanup, the primary net: ``show --session <sid>`` for each session id the scenario
   ran must answer ``unknown_session``. A binding in any state fails it by name, even one that ended with a clean
   teardown and so is on no list; any other answer, or none, fails it too.
3. :func:`check`, after cleanup, the second net: ``show --all`` (every binding on this host that is live or lapsed) is read
   before the sessions start and again after cleanup. A new live or lapsed identity fails the scenario when it is the
   sandbox's (its name is one of the scenario's session names, or its pid one the run recorded); any other new row is a
   note, since the operator may start sessions while a scenario runs, and a delegated identity is ignored (the operator's own
   ``claude -p`` children make them; a sandbox session cannot). An identity that ended is not on this list: check 2 covers it.

The sandbox's nx shim also refuses the verbs that make or change an identity (run.py ``nx_shim_text``), so a restart's
seat creation cannot add a row the lists do not show.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Collection, Iterable

TIMEOUT_S = 60

_ROW = re.compile(r"^\s+(i\d+)\s")
_FIELDS = re.compile(r"^\s+i\d+ (?P<name>.*) \((?P<kind>[a-z]+)\) \w+, generation \d+, pid (?P<pid>\d+)")
_SHELLS = {"sh", "bash", "dash", "zsh", "env"}


class CensusError(Exception):
    """The identity list could not be read, or did not read as the binding list."""


def parse(text: str) -> dict[str, str]:
    """Identity id -> its line, from ``nx identity show --all`` output. Raises CensusError for text that is not that list."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("bindings on "):
        raise CensusError(f"not a binding list: {text[:120]!r}")
    found: dict[str, str] = {}
    for line in lines[1:]:
        if not line.startswith((" ", "\t")):
            break  # the next section (live Claude processes with no binding)
        if line.strip() == "(none)":
            continue
        m = _ROW.match(line)
        if m is None:
            raise CensusError(f"a binding line that does not parse: {line[:120]!r}")
        found[m.group(1)] = line.strip()
    return found


def row_fields(row: str) -> tuple[str, str, int | None]:
    """(name, kind, holder pid) of one binding line; ("", "", None) for a line of another shape."""
    m = _FIELDS.match("  " + row)
    return (m["name"], m["kind"], int(m["pid"])) if m else ("", "", None)


def read(nx: str, *, timeout: float = TIMEOUT_S) -> dict[str, str]:
    """The operator's identities now, by one read-only ``nx identity show --all``. Raises CensusError on anything but a list."""
    if not nx:
        raise CensusError("no nx on PATH to ask")
    try:
        r = subprocess.run([nx, "identity", "show", "--all"], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CensusError(f"nx identity show --all did not answer within {timeout:.0f} s") from exc
    except OSError as exc:
        raise CensusError(f"nx identity show --all did not run: {exc}") from exc
    if r.returncode != 0:
        raise CensusError(f"nx identity show --all exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return parse(r.stdout)


def _number(identity: str) -> int:
    return int(identity[1:])


def new_identities(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The ids in *after* that are not in *before*, in numeric order."""
    return sorted(set(after) - set(before), key=_number)


def check(nx: str, before: dict[str, str], *, names: Collection[str] = (), pids: Collection[int] = ()) -> tuple[list[str], list[str]]:
    """Read the list again and compare with *before*: (failures, notes). An unreadable list is a failure. A new identity is
    the sandbox's (a failure) when its name is in *names* or its pid in *pids*; a delegated one is ignored; any other is a note."""
    try:
        after = read(nx)
    except CensusError as exc:
        return [f"identity census: the operator's identity list could not be read after the run ({exc}), so the sandbox "
                f"cannot be shown to have bound nothing"], []
    new = new_identities(before, after)
    notes = [f"identity census: {len(before)} identities before, {len(after)} after, {len(new)} new"]
    ours: list[str] = []
    for ident in new:
        name, kind, pid = row_fields(after[ident])
        if kind == "delegated":
            notes.append(f"identity census: new delegated identity ignored (a claude -p child of some session, never the sandbox's): {after[ident]}")
        elif name in names or (pid is not None and pid in pids):
            ours.append(ident)
        else:
            notes.append(f"identity census: new identity not attributed to the sandbox (the operator's own session started "
                         f"meanwhile?): {after[ident]}")
    if not ours:
        return [], notes
    return [f"identity census: the sandbox bound an identity on the operator's endpoint: {', '.join(ours)} "
            f"({'; '.join(after[i] for i in ours)})"], notes


def session_findings(nx: str, sids: Iterable[str], *, expect: bool = False, timeout: float = TIMEOUT_S) -> tuple[list[str], list[str]]:
    """``nx identity show --session <sid> --json`` for each session id: it must answer ``unknown_session`` (the answer the
    restart script reads the same way). A binding in any state fails by name; any other answer, or none, fails closed.
    *expect*: sessions ran, so no id to check means this examined nothing, which is a failure."""
    sids = sorted({s for s in sids if s})
    failures: list[str] = []
    if expect and not sids:
        failures.append("identity census: sessions ran but no session id was collected, so no session could be checked for a binding")
    if sids and not nx:
        return failures + ["identity census: no nx on PATH to ask whether any session bound an identity"], []
    for sid in sids:
        try:
            r = subprocess.run([nx, "identity", "show", "--session", sid, "--json"], capture_output=True, text=True,
                               timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"identity census: session {sid[:8]}: not read ({type(exc).__name__}: {exc})")
            continue
        said = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            try:
                view = json.loads(r.stdout)
                what = f"{view.get('id')} ({view.get('state')}, {view.get('kind')}, name {view.get('name') or view.get('display_name')})"
            except (ValueError, AttributeError):
                what = "an identity (its record did not parse)"
            failures.append(f"identity census: the sandbox bound an identity on the operator's endpoint: session {sid[:8]} is "
                            f"bound to {what}")
        elif "unknown_session" not in said:
            failures.append(f"identity census: session {sid[:8]}: not read, nx exited {r.returncode}: {said.strip()[:160]}")
    return failures, [f"identity census: {len(sids)} session id(s) asked, each must answer unknown_session"]


def judge(nx: str, before: dict[str, str], *, sids: Iterable[str], pids: Collection[int], names: Collection[str],
          expect: bool = False) -> tuple[list[str], list[str]]:
    """After a scenario: the per-session check (check 2, strict) and the list diff (check 3). (failures, notes)."""
    by_session, notes = session_findings(nx, sids, expect=expect)
    by_list, list_notes = check(nx, before, names=names, pids=pids)
    return by_session + by_list, notes + list_notes


def installed_python(nx: str) -> str:
    """The interpreter of the INSTALLED nx: the shebang of its entry script, following nexus's own generated wrapper
    (``exec "$NX_GEN/bin/nx"``, NX_GEN the target of a ``current`` pointer) to it. Raises CensusError when it cannot be found."""
    path = os.path.realpath(nx)
    for _ in range(3):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError as exc:
            raise CensusError(f"{path} not read: {exc}") from exc
        first = head.splitlines()[0] if head else ""
        if not first.startswith("#!") or not first[2:].split():
            raise CensusError(f"{path} has no interpreter line")
        interp = first[2:].split()[0]
        if os.path.basename(interp) not in _SHELLS:
            if not os.path.isabs(interp) or not os.path.exists(interp):
                raise CensusError(f"{path} names an interpreter that is not an existing absolute path: {interp}")
            return interp
        m = re.search(r'NX_GEN="\$\(readlink "([^"]+)"\)"', head)
        if m is None or '"$NX_GEN/bin/nx"' not in head:
            raise CensusError(f"{path} is a shell script that is not nexus's generated wrapper")
        pointer = m.group(1)
        try:
            gen = os.path.join(os.path.dirname(pointer), os.readlink(pointer))
        except OSError as exc:
            raise CensusError(f"{pointer} not read: {exc}") from exc
        path = os.path.realpath(os.path.join(gen, "bin", "nx"))
    raise CensusError(f"{nx} wraps another wrapper too deep to follow")


_PROBE = "import nexus.hooks.identity_bind as m; print(m.NX_IDENTITY_BIND_ENV)"
_BIND_NOTE = "the sandbox would bind identities on the operator's endpoint"


def bind_switch_gap(nx: str, *, timeout: float = TIMEOUT_S) -> str:
    """"" when the installed nx's SessionStart identity hook honours ``NX_IDENTITY_BIND``; else why this run must end
    unverified with no session started. Asks the installed nx's own interpreter (``-I``: not this checkout, not PYTHONPATH)
    to import the hook and name the variable it reads. An nx that cannot be asked is a gap too: unproven is not safe."""
    if not nx:
        return f"no nx on PATH to ask whether it honours NX_IDENTITY_BIND; {_BIND_NOTE}"
    try:
        py = installed_python(nx)
        r = subprocess.run([py, "-I", "-c", _PROBE], capture_output=True, text=True, timeout=timeout, check=False)
    except CensusError as exc:
        return f"could not find the installed nx's interpreter to ask whether it honours NX_IDENTITY_BIND ({exc}); {_BIND_NOTE}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not ask the installed nx whether it honours NX_IDENTITY_BIND ({type(exc).__name__}: {exc}); {_BIND_NOTE}"
    if r.returncode == 0 and r.stdout.strip() == "NX_IDENTITY_BIND":
        return ""
    if "NX_IDENTITY_BIND_ENV" in r.stderr:
        return f"installed nx predates NX_IDENTITY_BIND; {_BIND_NOTE}"
    return (f"could not ask the installed nx whether it honours NX_IDENTITY_BIND (exit {r.returncode}: "
            f"{(r.stderr or r.stdout).strip()[-160:]}); {_BIND_NOTE}")
