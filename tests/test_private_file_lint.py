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


def _is_owner_mode(node: ast.expr, names: frozenset[str] = frozenset()) -> bool:
    """``0o600`` as a literal, as ``stat.S_IRUSR | stat.S_IWUSR``, or as a name
    the file bound to one of those (``MODE = 0o600``)."""
    if isinstance(node, ast.Constant):
        return node.value == 0o600 and not isinstance(node.value, bool)
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        parts = {_stat_flag(node.left), _stat_flag(node.right)}
        return parts == {"S_IRUSR", "S_IWUSR"}
    return False


def _stat_flag(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr in ("S_IRUSR", "S_IWUSR"):
        return node.attr
    if isinstance(node, ast.Name) and node.id in ("S_IRUSR", "S_IWUSR"):
        return node.id
    return None


def _owner_mode_names(tree: ast.AST) -> frozenset[str]:
    """Names this file binds to an owner-only mode, anywhere in it (flow
    insensitive: ``MODE = 0o600`` at module level or in a function). A mode
    imported from another module is not followed."""
    names: set[str] = set()
    changed = True
    while changed:  # ``B = A`` where A is already a mode
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = [t for t in node.targets if isinstance(t, ast.Name)], node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if _is_owner_mode(value, frozenset(names)):
                for t in targets:
                    if t.id not in names:
                        names.add(t.id)
                        changed = True
    return frozenset(names)


def scan_source(text: str) -> Counter[str]:
    """Enclosing-function name -> count of 0o600 / fchmod calls in *text*."""
    found: Counter[str] = Counter()
    tree = ast.parse(text)
    mode_names = _owner_mode_names(tree)
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
        if not (is_fchmod or any(_is_owner_mode(a, mode_names) for a in args)):
            continue
        scope: ast.AST = node
        while scope in parent and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parent[scope]
        name = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
        found[name] += 1
    return found


def _sites() -> tuple[Counter[tuple[str, str]], int]:
    found: Counter[tuple[str, str]] = Counter()
    files = 0
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            files += 1
            rel = str(path.relative_to(REPO))
            for name, count in scan_source(path.read_text(encoding="utf-8")).items():
                found[(rel, name)] += count
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


# ── planted violations: the sweep must be able to fail ───────────────────────────


def test_a_literal_0o600_call_is_caught() -> None:
    assert scan_source("import os\ndef w(p):\n    return os.open(p, 1, 0o600)\n") == {"w": 1}


def test_a_module_constant_mode_is_caught() -> None:
    src = "import os\nMODE = 0o600\ndef w(p, f):\n    return os.open(p, f, MODE)\n"
    assert scan_source(src) == {"w": 1}


def test_a_function_local_constant_mode_is_caught() -> None:
    src = "import os\ndef w(p, f):\n    _M = 0o600\n    os.chmod(p, _M)\n"
    assert scan_source(src) == {"w": 1}


def test_an_annotated_constant_mode_is_caught() -> None:
    src = "import os\nfrom typing import Final\nMODE: Final[int] = 0o600\ndef w(p):\n    os.chmod(p, MODE)\n"
    assert scan_source(src) == {"w": 1}


def test_an_alias_of_a_constant_mode_is_caught() -> None:
    src = "import os\nA = 0o600\nB = A\ndef w(p):\n    os.chmod(p, B)\n"
    assert scan_source(src) == {"w": 1}


def test_stat_flags_that_spell_0o600_are_caught() -> None:
    src = "import os, stat\ndef w(p):\n    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)\n"
    assert scan_source(src) == {"w": 1}


def test_a_keyword_mode_is_caught() -> None:
    src = "import os\nM = 0o600\ndef w(p):\n    os.open(p, 1, mode=M)\n"
    assert scan_source(src) == {"w": 1}


def test_other_modes_and_unrelated_names_are_not_flagged() -> None:
    src = (
        "import os\n"
        "MODE = 0o644\nOTHER = 0o600\n"
        "def w(p, MODE_UNUSED):\n"
        "    os.chmod(p, MODE)\n"          # 0o644, not owner-only
        "    return len(p)\n"
    )
    assert scan_source(src) == {}


def test_fchmod_is_caught_whatever_its_mode() -> None:
    assert scan_source("import os\ndef w(fd):\n    os.fchmod(fd, 0o644)\n") == {"w": 1}
