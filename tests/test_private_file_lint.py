# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ``0o600`` credential write goes through ``nexus._winsec`` (RDR-224, nexus-f9bgu.22).

On Windows ``os.open(path, flags, 0o600)`` and ``os.chmod(path, 0o600)`` restrict
nothing, and ``os.fchmod`` does not exist. A credential file written that way is
readable by every local account. ``open_private`` / ``restrict_to_owner`` are the
one place that knows both platforms.

The sweep finds every CALL that passes the literal ``0o600`` (by AST, so aliases
and comments do not matter) under the wheel and the plugin's hook scripts, and
every ``fchmod`` call. Each must be either inside ``_winsec.py`` itself or in
``NON_CREDENTIAL`` below with the reason it holds no secret. A new site fails
until it routes through the helper or is classified here, which is the point:
the classification is a decision somebody makes, not a default.

Exemptions are keyed on (file, enclosing function) with an exact count, so a
moved or removed exemption fails as loudly as a new violation.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).parent.parent
SCAN_ROOTS = ("src/nexus", "conexus/hooks/scripts")

#: The helper's own two calls: the POSIX arms.
HELPER: dict[tuple[str, str], int] = {
    ("src/nexus/_winsec.py", "open_private"): 1,
    ("src/nexus/_winsec.py", "restrict_to_owner"): 1,
}

#: Sites that pass ``0o600`` and hold no secret, each with its reason. A lock
#: file, a pid, an identifier or a handoff marker is private by habit, not
#: because anything in it authorizes anything.
NON_CREDENTIAL: dict[tuple[str, str], int] = {
    # Lock files: empty, flocked, never read for content.
    ("conexus/hooks/scripts/mailbox_drain.py", "_pending_lock"): 1,
    ("src/nexus/hooks/mailbox_drain.py", "_pending_lock"): 1,
    ("src/nexus/config.py", "_config_write_lock"): 1,
    ("src/nexus/daemon/service_registry.py", "_open"): 1,  # the election flock
    ("src/nexus/daemon/storage_service_daemon.py", "start"): 1,
    ("src/nexus/db/data_token.py", "_mint_guarded"): 1,
    ("src/nexus/db/t1.py", "_lock_guarded_mint_or_borrow"): 1,
    ("src/nexus/db/t1.py", "clear_t1_session_lease_if_matches"): 1,
    ("src/nexus/upgrade_ladder/rungs/rdr192_manifest_backfill.py", "_cross_process_lock"): 1,
    # PID files and a restart-timestamp sentinel.
    ("src/nexus/_mineru_spawn.py", "_write_pid_file"): 1,
    ("src/nexus/commands/console.py", "_write_pid_file"): 1,
    ("src/nexus/commands/daemon.py", "_write_crashloop_atomic"): 1,
    # Session identifiers (a UUID names a scope; the session TOKEN lives in the lease the helper writes).
    ("src/nexus/session.py", "write_claude_session_id"): 1,
    ("src/nexus/db/t1.py", "_cli_dedicated_session_id"): 2,
    # Handoff markers: a new session id, a pid and a timestamp.
    ("src/nexus/daemon/t1_handoff.py", "write_handoff_marker"): 1,
    ("src/nexus/daemon/t1_handoff.py", "write_handoff_marker_if_absent"): 1,
}

#: Non-vacuity floor: files the sweep must read.
MIN_FILES = 150


def _sites() -> tuple[Counter[tuple[str, str]], int]:
    found: Counter[tuple[str, str]] = Counter()
    files = 0
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            files += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parent: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parent[child] = node
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                args = [*node.args, *(k.value for k in node.keywords)]
                func = node.func
                is_fchmod = (isinstance(func, ast.Attribute) and func.attr == "fchmod") or (
                    isinstance(func, ast.Name) and func.id == "fchmod"
                )
                if not (is_fchmod or any(isinstance(a, ast.Constant) and a.value == 0o600 for a in args)):
                    continue
                scope: ast.AST = node
                while scope in parent and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = parent[scope]
                name = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
                found[(str(path.relative_to(REPO)), name)] += 1
    return found, files


def test_every_0o600_call_is_the_helper_or_a_classified_non_credential() -> None:
    found, files = _sites()
    assert files >= MIN_FILES, f"the sweep read only {files} files; its roots moved"
    allowed = {**HELPER, **NON_CREDENTIAL}
    unclassified = {k: v for k, v in found.items() if k not in allowed}
    assert not unclassified, (
        "a 0o600 / fchmod call outside nexus._winsec: route a credential write through "
        "open_private / restrict_to_owner (Windows ignores 0o600 and has no fchmod), or classify a "
        f"non-credential site in NON_CREDENTIAL with its reason: {unclassified}"
    )
    drifted = {k: (allowed[k], found.get(k, 0)) for k in allowed if found.get(k, 0) != allowed[k]}
    assert not drifted, f"an exemption no longer matches the code (expected, found): {drifted}"


def test_the_helper_arms_are_what_the_sweep_found() -> None:
    found, _ = _sites()
    assert {k: found[k] for k in HELPER} == HELPER, "non-vacuity: the sweep must see the helper's own two POSIX calls"
