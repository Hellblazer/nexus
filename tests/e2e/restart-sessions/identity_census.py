# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A census of the operator's RDR-253 identities, to prove the restart sandbox bound none (found 2026-11-19).

run.py loads this file by path (``_load_slash("identity_census")``), as it loads sandbox_work.py: it runs under ``python -I``,
which leaves its own directory off sys.path. The sandbox's sessions carry a copy of the operator's engine credential, and
before ``NX_IDENTITY_BIND=0`` / ``NX_IDENTITY_LEASE=0`` went into their environment each one bound an identity in the
operator's tenant (twelve ``rs_*`` rows). The switches are the fix; this is the check that they hold: the list is read
before the sessions start and again after cleanup, and any identity in the second list that was not in the first fails
the run. A list that cannot be read fails it too: a gate that cannot look is a failure, not a pass.

The read is ``nx identity show --all`` (the run's ``real_nx``, in the operator's own environment): every binding on this
host, one line each, read only. The engine has no route that lists seats nobody holds, which the sandbox never makes.
A session of the operator's own that starts on this host while a scenario runs is also new here, and fails it: rerun.
"""
from __future__ import annotations

import re
import subprocess

TIMEOUT_S = 60

_ROW = re.compile(r"^\s+(i\d+)\s")


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


def check(nx: str, before: dict[str, str]) -> tuple[list[str], list[str]]:
    """Read the list again and compare with *before*: (failures, notes). An unreadable list is a failure."""
    try:
        after = read(nx)
    except CensusError as exc:
        return [f"identity census: the operator's identity list could not be read after the run ({exc}), so the sandbox "
                f"cannot be shown to have bound nothing"], []
    new = new_identities(before, after)
    notes = [f"identity census: {len(before)} identities before, {len(after)} after, {len(new)} new"]
    if not new:
        return [], notes
    rows = "; ".join(after[i] for i in new)
    return [f"identity census: the sandbox bound an identity on the operator's endpoint: {', '.join(new)} ({rows})"], notes
