#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The behaviour leg of the plugin out-of-repo gate (nexus-sby49.3, plan section 5 item 2).

The static pass reads the plugin's markdown. This leg RUNS its hooks, the way a user's machine does, and asks the
question the audience manifest answers: does a user who is not nexus's maintainer get a quiet, neutral plugin, and does
nexus's own checkout still get the full one? Deterministic: it needs ``uv`` and the project's Python, never ``claude``.

It runs each ``hooks.json`` registration's real argv, with ``${CLAUDE_PLUGIN_ROOT}`` substituted, under payloads of the
shape Claude Code sends, in three fixtures:

* ``plain``: a scratch git repository (clean tree) on an account with no flag: a user's repository. ``plain-dirty`` is
  the same with an uncommitted change, for the git guard's dirty-tree rows.
* ``marked``: a scratch repository whose tracked files carry the nexus markers, on an account with no flag.
* ``flagged``: a plain repository on an account that carries the maintainer flag.

and two postures:

* ``P0``: no ``nx`` on PATH. The nx-launcher and shim entries must exit 0 and print nothing on standard output.
* ``P2``: the checkout's ``nx-hook`` (or, with ``--nx installed``, the installed tool's), reached through a shim on PATH.

What it asserts, and where each assertion lives:

* in a plain fixture the only denies are the ones the fixture's probe expects (D1's list, the credential read, a Stop
  promise), and no output carries the lexicon (a bead id, an RDR id, ``bd``, Sam, Hal, peer names, ``orchestrator``,
  ``develop/main``, the lab hosts, ``Hellblazer/nexus``, ``hook-ENFORCED``, ``local AND cloud``);
* in the marked and flagged fixtures every live SAM-WIDE constant appears in some output, every live NEXUS-REPO constant
  appears in the marked fixture and not in the flagged one, and none of them appears in a plain one;
* a probe that names the decision it expects (the git guard's rows of plan section 2.1 per audience) gets it.

A finding that is not a failure is allowlisted in ``plugin-behaviour-allowlist.txt``, and only for a manifest row that is
not live yet (a Phase 3 row). An entry whose condition has passed fails, and so does an entry that matched nothing.

Account control. The helper reads the account home from the OS account database, so no environment variable moves it.
With ``--account auto`` the leg runs the real argv when the real account has no flag (a CI runner, a lab host), and when
it has one (a maintainer's laptop) it inserts ``tests/hooks/audience_harness.py`` after ``python``, which sets the
in-process seam and runs the same script; the output says which. The src hooks reach the seam through a prelude in the
P2 shim, in every mode.

Usage (from the repository, under the project's interpreter; the gate script does this)::

    uv run python tests/e2e/lib/plugin_behaviour.py run --plugin conexus [--posture P0|P2|both] [--nx checkout|installed]
        [--allowlist FILE] [--account auto|real|harness] [--only SUBSTR] [--jobs N] [--keep]

Exit: 0 passed, 1 failed, 2 unverified (no ``uv``, no pyyaml, a plugin with no manifest).
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import dataclasses
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests import _audience_manifest as am  # noqa: E402

HARNESS = REPO / "tests" / "hooks" / "audience_harness.py"
DEFAULT_ALLOWLIST = REPO / "tests" / "e2e" / "plugin-behaviour-allowlist.txt"
SEAM_ENV = "NX_TEST_ACCOUNT_HOME"
NX_PROGRAMS = ("nx", "nx-hook", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher")

#: A user's output must carry none of this (plan section 5 item 2). ``Sam`` and ``Hal`` are case-sensitive; the rest are not.
#: The peer names are the person names in the stop guard's ``_PEER_NAMES``; the rest of that tuple is ordinary English.
PEER_NAMES = ("bob", "linda", "wren", "halcyon", "kimi", "marlowe", "tamsin", "steve", "steven", "kestrel", "elsinore", "labrun")
LEXICON_CS = re.compile(r"\b(?:Sam|Hal)\b")
LEXICON_CI = re.compile(
    r"\bnexus-[a-z0-9]{4,}\b|\bRDR-\d+\b|\bbd\b|\borchestrator\b|develop/main|\blab-remote\b|\blab-run\b|\bchas\b|\bqwent\b"
    r"|\bhellmini\b|Hellblazer/nexus|hook-ENFORCED|local AND cloud|\b(?:" + "|".join(PEER_NAMES) + r")\b",
    re.IGNORECASE,
)


def lexicon_hits(text: str, *, beads: bool = False) -> list[str]:
    """Every lexicon term in *text*, as written, in order of appearance. With *beads* (the project has a ``.beads/``
    directory: a data gate that is on) the word ``bd`` is not a hit."""
    found = [(m.start(), m.group(0)) for m in LEXICON_CS.finditer(text)] + [(m.start(), m.group(0)) for m in LEXICON_CI.finditer(text)]
    return [t for _, t in sorted(found) if not (beads and t.lower() == "bd")]


# --- section 2.1: the git guard per verb, for a user and for the maintainer ---------------------------
#: (command, user, maintainer): "deny" or "pass". A user's dirty-tree rows are in ``DIRTY_ROWS``.
GUARD_ROWS: list[tuple[str, str, str]] = [
    ("git commit -am x", "pass", "deny"),
    ("git add f", "pass", "deny"),
    ("git push origin main", "pass", "deny"),
    ("git push -f origin main", "deny", "deny"),
    ("git push --force-with-lease origin main", "deny", "deny"),
    ("git push --force-if-includes origin main", "deny", "deny"),
    ("git push origin +main", "deny", "deny"),
    ("git push origin --delete foo", "deny", "deny"),
    ("git push origin :foo", "deny", "deny"),
    ("git push --mirror origin", "deny", "deny"),
    ("git push --prune origin", "deny", "deny"),
    ("git rebase main", "pass", "deny"),
    ("git merge other", "pass", "deny"),
    ("git pull", "pass", "deny"),
    ("git cherry-pick abc1234", "pass", "deny"),
    ("git revert abc1234", "pass", "deny"),
    ("git branch newb", "pass", "pass"),
    ("git branch -d foo", "pass", "deny"),
    ("git branch -D foo", "deny", "deny"),
    ("git branch --delete --force foo", "deny", "deny"),
    ("git branch -M a b", "deny", "deny"),
    ("git branch -f x HEAD", "pass", "deny"),
    ("git tag v1", "pass", "deny"),
    ("git tag -d v1", "pass", "deny"),
    ("git config core.hooksPath /tmp/x", "deny", "deny"),
    ("git config " + "--glob" + "al user.name x", "deny", "deny"),
    ("git config user.email a@b", "pass", "deny"),
    ("git config --get user.name", "pass", "pass"),
    ("git reflog expire --all", "deny", "deny"),
    ("git reflog delete HEAD@{1}", "deny", "deny"),
    ("git gc --prune=now", "deny", "deny"),
    ("git prune", "deny", "deny"),
    ("git update-ref -d refs/heads/x", "deny", "deny"),
    ("git update-ref refs/heads/x HEAD", "pass", "deny"),
    ("git filter-branch --all", "deny", "deny"),
    ("git fetch origin", "pass", "pass"),
    ("git worktree add ../wt", "pass", "deny"),
    ("git worktree remove --force ../wt", "deny", "deny"),
    ("git worktree prune", "pass", "deny"),
    ("git mv a b", "pass", "deny"),
    ("git rm f", "pass", "deny"),
    ("git rm --cached f", "pass", "deny"),
    ("git checkout main", "pass", "deny"),
    ("git switch main", "pass", "deny"),
    ("git restore --staged f", "pass", "deny"),
    ("git reset --soft HEAD~1", "pass", "deny"),
    ("git reset --mixed HEAD", "pass", "deny"),
    ("git stash pop", "pass", "deny"),
    ("git stash drop", "deny", "deny"),
    ("git stash clear", "deny", "deny"),
    ('echo "git reset --hard"', "pass", "deny"),
    ("git log --grep=reset", "pass", "deny"),
    ("git status", "pass", "pass"),
    ("git log", "pass", "pass"),
    ("git diff", "pass", "pass"),
    ("git show HEAD", "pass", "pass"),
    ("gh pr merge 5", "pass", "pass"),
    ("gh pr checkout 5", "pass", "deny"),
]
#: Rows that deny for a user only when the tree has something to lose; the plain fixture is clean, plain-dirty is not.
DIRTY_ROWS: list[str] = [
    "git checkout -- f", "git checkout -f main", "git restore f", "git reset --hard", "git reset --hard HEAD~1",
    "git stash", "git stash -u", "git rm -f f", "git clean -fd",
]
CREDENTIAL_ROWS = [("cat /proc/1234/environ", "deny"), ("ls -la", "pass")]
#: (command, what the output is): advice is additionalContext with no decision, none is silence. A user's advice is as neutral
#: as the maintainer's (U8a), and no fixture's output is a deny (U8b: the flip is not made, and never reaches a user).
ADVICE_ROWS = [("pkill -f pytest", "advice"), ("find / -name x.py", "advice"), ("ls -la", "none")]
#: U2b: the tools a user is prompted for (the hook is silent, so Claude Code's own prompt decides); the maintainer's account allows.
PROMPTED_FOR_USERS = ("memory_delete", "plan_delete", "memory_consolidate", "nx_tidy", "tuple_in", "mailbox_send")
#: U6b: the stop guard's CI wording, which only a nexus checkout reads.
CI_TEXT = ("nx ci wait", "CI board", "develop and main")

FIXTURES = ("plain", "plain-dirty", "plain-beads", "marked", "flagged")
USER_FIXTURES = ("plain", "plain-dirty", "plain-beads")
#: A user's repository with a ``.beads/`` directory: only the compaction events run in it (S2-2's data gate, switched on).
BEADS_FIXTURES = ("plain-beads",)
#: The interpreter that runs the module-driven probes (SubagentStart): the project's, or the installed tool's with ``--nx installed``.
MODULE_PYTHON = sys.executable


# --- data ---------------------------------------------------------------------------------------


@dataclasses.dataclass
class Fixture:
    name: str
    repo: Path
    home: Path
    flagged_home: bool


@dataclasses.dataclass
class Probe:
    id: str
    reg_key: str
    fixture: str
    posture: str
    kind: str                       # script | nx | module
    argv: list[str]                 # for script and nx probes; empty for module
    payload: dict | None
    expect: str | None = None       # "deny" | "pass" | "block" | "allow" | "advice" | "none" | None (no expectation beyond the fixture rules)
    group: str = ""                 # probes of one group run in order (by seq), in one worker
    module_driver: str = ""         # for kind == module: the driver's source
    stdin_raw: str | None = None
    seq: int = 0
    must: tuple[str, ...] = ()      # the output carries each of these
    forbid: tuple[str, ...] = ()    # the output carries none of these
    private_cfg: bool = False       # run with a fresh, empty config dir and fail if the hook leaves anything in it (U6c)


@dataclasses.dataclass
class Outcome:
    probe: Probe
    rc: int
    stdout: str
    stderr: str
    decision: str
    text: str
    note: str = ""
    leaked: tuple[str, ...] = ()    # what a private_cfg probe's hook left in its empty config dir


# --- output classification ------------------------------------------------------------------------


def _leaves(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _leaves(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _leaves(v)]
    return []


def classify(stdout: str) -> tuple[str, str]:
    """``(decision, text)``: deny | ask | allow | block | advice | text | none, and every string the output carries."""
    raw = stdout.strip()
    if not raw:
        return "none", ""
    try:
        data = json.loads(raw)
    except ValueError:
        return "text", stdout
    if not isinstance(data, dict):
        return "text", stdout
    text = "\n".join(_leaves(data))
    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict):
        verdict = specific.get("permissionDecision")
        if verdict in ("deny", "ask", "allow"):
            return str(verdict), text
        if specific.get("additionalContext"):
            return "advice", text
    if data.get("decision") == "block":
        return "block", text
    if isinstance(data.get("hookSpecificOutput"), dict) and data["hookSpecificOutput"].get("decision"):
        return "allow", text
    return "text", text


# --- the allowlist --------------------------------------------------------------------------------


@dataclasses.dataclass
class AllowEntry:
    row: str
    hit: str
    until: str
    reason: str
    used: int = 0


def parse_allowlist(text: str) -> list[AllowEntry]:
    """``row <TAB> hit <TAB> until: phase-N | D<n> <TAB> reason``; ``#`` starts a comment line."""
    out: list[AllowEntry] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4 or not parts[2].startswith("until: "):
            raise ValueError(f"allowlist line {n}: want 'row<TAB>hit<TAB>until: phase-N or D<n><TAB>reason'")
        out.append(AllowEntry(parts[0].strip(), parts[1].strip(), parts[2][len("until: "):].strip(), parts[3].strip()))
    return out


def allowlist_problems(entries: list[AllowEntry], manifest: dict) -> list[str]:
    """Entries that name a row that is live, a row that does not exist, or a condition that has passed."""
    out: list[str] = []
    not_live = set(am.allowlist_rows(manifest))
    shipped = int(manifest.get("phase_shipped", 0))
    answered = set(manifest.get("decisions_answered") or [])
    for e in entries:
        if e.row not in not_live:
            out.append(f"allowlist row {e.row!r} is not a row that is not live yet (only the manifest's live: false rows may be allowlisted)")
        m = re.fullmatch(r"phase-(\d+)", e.until)
        if m:
            if int(m.group(1)) <= shipped:
                out.append(f"allowlist entry {e.row!r} / {e.hit!r}: until {e.until} has passed (phase {shipped} shipped)")
        elif re.fullmatch(r"D\d+", e.until):
            if e.until in answered:
                out.append(f"allowlist entry {e.row!r} / {e.hit!r}: {e.until} is answered, the entry is past its condition")
        else:
            out.append(f"allowlist entry {e.row!r}: unknown condition {e.until!r}")
        if not e.reason:
            out.append(f"allowlist entry {e.row!r} / {e.hit!r}: no reason")
    return out


def _row_covers(row: str, reg_key: str, manifest: dict) -> bool:
    """Does the manifest row *row* (a registration key, a constant id or a rule id) belong to registration *reg_key*?"""
    regs = {r["key"]: r for r in manifest["registrations"]}
    if row == reg_key:
        return True
    module = None
    if any(c["id"] == row for c in manifest.get("constants") or []):
        module = row.rsplit(":", 1)[0]
    else:
        for r in manifest.get("rules") or []:
            if r["id"] == row:
                module = r["module"]
    return bool(module and module in (regs.get(reg_key, {}).get("modules") or []))


def apply_allowlist(findings: list[dict], entries: list[AllowEntry], manifest: dict) -> tuple[list[dict], list[dict]]:
    """Split *findings* into ``(failures, allowlisted)``; each is ``{reg_key, hit, ...}``."""
    failures: list[dict] = []
    allowed: list[dict] = []
    for f in findings:
        for e in entries:
            if f["hit"] == e.hit and _row_covers(e.row, f["reg_key"], manifest):
                e.used += 1
                allowed.append(f)
                break
        else:
            failures.append(f)
    return failures, allowed


# --- constants ----------------------------------------------------------------------------------


def constant_text(repo: Path, constant_id: str) -> str | None:
    """The literal value of ``<module path>:<NAME>``, read from the source (no import), or None."""
    module, name = constant_id.rsplit(":", 1)
    path = am.module_file(repo, module)
    if not path.is_file():
        return None
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets) and getattr(node, "value", None) is not None:
            try:
                value = ast.literal_eval(node.value)
            except Exception:  # noqa: BLE001
                return None
            return value if isinstance(value, str) else None
    return None


def _norm(text: str) -> str:
    return " ".join(text.split())


def presence_problems(manifest: dict, repo: Path, texts: dict[str, str]) -> tuple[list[str], int]:
    """Where each live SAM-WIDE and NEXUS-REPO constant must and must not appear. Returns (problems, constants examined)."""
    out: list[str] = []
    examined = 0
    norm = {k: _norm(v) for k, v in texts.items()}
    users = "\n".join(norm.get(f, "") for f in USER_FIXTURES)
    for c in manifest.get("constants") or []:
        if not c.get("live", True) or c["audience"] not in ("SAM-WIDE", "NEXUS-REPO"):
            continue
        value = constant_text(repo, c["id"])
        if not value or not value.strip():
            out.append(f"{c['id']}: the constant's text could not be read from the source")
            continue
        needle = _norm(value)
        examined += 1
        in_marked, in_flagged = needle in norm.get("marked", ""), needle in norm.get("flagged", "")
        if c["audience"] == "SAM-WIDE":
            if not in_marked:
                out.append(f"{c['id']} (SAM-WIDE) never appeared in the marked fixture's output")
            if not in_flagged:
                out.append(f"{c['id']} (SAM-WIDE) never appeared in the flagged fixture's output")
        else:
            if not in_marked:
                out.append(f"{c['id']} (NEXUS-REPO) never appeared in the marked fixture's output")
            if in_flagged:
                out.append(f"{c['id']} (NEXUS-REPO) appeared in the flagged fixture, whose repo is not nexus")
        if needle in users:
            out.append(f"{c['id']} ({c['audience']}) appeared in a user's output")
    return out, examined


# --- scratch environment ----------------------------------------------------------------------------


def _inside_a_repository(path: Path) -> bool:
    d = path.resolve()
    while True:
        if (d / ".git").exists():
            return True
        if d == d.parent:
            return False
        d = d.parent


def scratch_root() -> str | None:
    """A directory with no git repository above it: the fixtures must be user repositories, not parts of this checkout.

    ``TMPDIR`` first, as the gate's other passes use. A lab-remote run puts ``TMPDIR`` inside its copy of the worktree,
    so the system temp directories come next."""
    for cand in (tempfile.gettempdir(), "/tmp", "/var/tmp", str(Path.home())):
        p = Path(cand)
        if p.is_dir() and os.access(p, os.W_OK) and not _inside_a_repository(p):
            return str(p)
    return None


def _uv() -> str | None:
    return shutil.which("uv")


def _account_flagged(plugin: Path) -> bool:
    """Is the REAL account flagged? Asked of the plugin's own helper, which reads the OS account database."""
    spec = importlib.util.spec_from_file_location("_audience_probe", plugin / "hooks" / "scripts" / "_audience.py")
    if spec is None or spec.loader is None:
        return False
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return bool(mod.account_flag())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=leg", "-c", "user.email=leg@example.invalid", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def make_fixtures(work: Path) -> dict[str, Fixture]:
    homes = {"plain": work / "home-plain", "flagged": work / "home-flagged"}
    for h in homes.values():
        h.mkdir(parents=True)
    flag = homes["flagged"].joinpath(*am_flag_parts())
    flag.parent.mkdir(parents=True)
    flag.write_text("")
    out: dict[str, Fixture] = {}
    for name in FIXTURES:
        repo = work / f"repo-{name}"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / "f").write_text("one\n")
        if name == "marked":
            (repo / ".nexus").mkdir()
            (repo / ".nexus" / "dev-process.json").write_text("{}")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "agent-worktree-preflight.sh").write_text("#!/bin/sh\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        if name in BEADS_FIXTURES:
            (repo / ".beads").mkdir()
        if name == "plain-dirty":
            (repo / "f").write_text("two\n")
            (repo / "untracked.txt").write_text("x\n")
        out[name] = Fixture(name, repo, homes["flagged"] if name == "flagged" else homes["plain"], name == "flagged")
    return out


def am_flag_parts() -> tuple[str, ...]:
    return (".config", "nexus", "maintainer-profile")


#: Programs the leg hides from every fixture's PATH: the nx family (P0 has no nx, and P2 brings its own through the shim
#: directory) and ``bd`` (a user's repository has no beads; with ``bd`` on PATH the SessionStart index line would name the
#: beads topic, which is a data gate that is on, not a leak).
HIDDEN_PROGRAMS = (*NX_PROGRAMS, "bd")


def sanitized_path(shim_dir: Path | None, work: Path) -> str:
    """PATH without the hidden programs. A directory that holds one is mirrored by symlinks to all its other entries, so
    ``uv``, ``git`` and the interpreters that live beside ``nx`` or ``bd`` stay reachable."""
    keep: list[str] = []
    for i, d in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not d:
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        if not any(n in names for n in HIDDEN_PROGRAMS):
            keep.append(d)
            continue
        mirror = work / "pathmirror" / str(i)
        mirror.mkdir(parents=True, exist_ok=True)
        for n in names:
            if n in HIDDEN_PROGRAMS or (mirror / n).exists():
                continue
            try:
                (mirror / n).symlink_to(Path(d) / n)
            except OSError:
                pass
        keep.append(str(mirror))
    if shim_dir is not None:
        keep.insert(0, str(shim_dir))
    return os.pathsep.join(keep)


def base_env(work: Path, plugin: Path, fx: Fixture, posture: str, shim_dir: Path | None, uv_env: dict[str, str]) -> dict[str, str]:
    scrub = ("NX_", "NEXUS_", "CLAUDE", "UV_", "XDG_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(scrub)}
    iso = work / "iso"
    for d in ("cfg", "nx-tools", "uv-tools", "uv-bin"):
        (iso / d).mkdir(parents=True, exist_ok=True)
    version = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text()).get("version", "0.0.0")
    (iso / "lockstep-marker").write_text(f"{version}\n")
    (iso / "installed_plugins.json").write_text('{"version": 2, "plugins": {}}\n')
    (iso / "known_marketplaces.json").write_text("{}\n")
    env.update({
        "PATH": sanitized_path(shim_dir if posture == "P2" else None, work),
        "HOME": str(fx.home),
        "NX_NO_TELEMETRY": "1", "NX_UPGRADE_AUTO": "0", "NX_DISPATCH_COUNTER": "off",
        "NEXUS_CONFIG_DIR": str(iso / "cfg"), "NX_TOOLS_DIR": str(iso / "nx-tools"),
        "UV_TOOL_DIR": str(iso / "uv-tools"), "UV_TOOL_BIN_DIR": str(iso / "uv-bin"),
        "NX_LOCKSTEP_MARKER": str(iso / "lockstep-marker"),
        "NX_PLUGIN_REGISTRY": str(iso / "installed_plugins.json"), "NX_PLUGIN_MARKETPLACES": str(iso / "known_marketplaces.json"),
        "NX_SERVICE_URL": "http://127.0.0.1:9", "NX_SERVICE_TOKEN": "plugin-gate-unused",
        "CLAUDE_PLUGIN_ROOT": str(plugin), "CLAUDE_PROJECT_DIR": str(fx.repo),
        **uv_env,
    })
    if posture == "P2":
        env["NX_HOOK_PYTHON"] = sys.executable
        env[SEAM_ENV] = str(fx.home)
    return env


def real_uv_env() -> dict[str, str]:
    """The uv cache and python install dir of the REAL home, pinned before HOME moves: a fresh home would re-resolve python."""
    out: dict[str, str] = {}
    uv = _uv()
    if uv is None:
        return out
    try:
        cache = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, timeout=30).stdout.strip()
        if cache:
            out["UV_CACHE_DIR"] = cache
        pydir = subprocess.run([uv, "python", "dir"], capture_output=True, text=True, timeout=30).stdout.strip()
        if pydir:
            out["UV_PYTHON_INSTALL_DIR"] = pydir
    except (OSError, subprocess.SubprocessError):
        pass
    return out


SHIM_PRELUDE = (
    "import os, sys\n"
    f"_h = os.environ.get({SEAM_ENV!r})\n"
    "if _h:\n"
    "    from nexus.hooks import _audience as _a\n"
    "    _a._ACCOUNT_HOME_OVERRIDE = _h\n"
)


def write_shims(shim_dir: Path, python: str) -> None:
    """``nx``, ``nx-hook`` and ``nx-session-end-launcher`` that run the checkout's (or the installed tool's) code under the seam."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "nx-hook": SHIM_PRELUDE + "from nexus._hook_runtime.entry import main\nmain()\n",
        "nx-session-end-launcher": SHIM_PRELUDE + "from nexus._session_end_launcher import main\nmain()\n",
        # P2 is a healthy install: the preflight verb looks for ``nx`` itself and prints its failure marker without it.
        "nx": SHIM_PRELUDE + "from nexus.cli import main\nmain()\n",
    }
    for name, code in body.items():
        main_py = shim_dir / f"_{name.replace('-', '_')}_main.py"
        main_py.write_text(code, encoding="utf-8")
        script = shim_dir / name
        script.write_text(f"#!/bin/sh\nexec '{python}' '{main_py}' \"$@\"\n", encoding="utf-8")
        script.chmod(0o755)


def installed_python() -> str | None:
    """The interpreter of the installed ``nx-hook`` (its shebang), for ``--nx installed``."""
    exe = shutil.which("nx-hook")
    if exe is None:
        return None
    try:
        first = Path(exe).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    return first[2:].strip().split()[0] if first.startswith("#!") else None


SUBAGENT_START_DRIVER = SHIM_PRELUDE + (
    "import json\n"
    "from nexus._hook_runtime._io import never_fail\n"
    "from nexus.hooks import subagent_start\n"
    "payload = json.loads(sys.stdin.read())\n"
    "result = never_fail(lambda: subagent_start.run(payload), 'subagent_start')\n"
    "sys.stdout.write(result.stdout or '')\n"
)


# --- probes -------------------------------------------------------------------------------------------


def _python_index(args: list[str]) -> int:
    return args.index("python") if "python" in args else -1


def entry_argv(plugin: Path, hook: dict, *, harness_home: Path | None) -> list[str]:
    """The registration's real argv. With *harness_home*, the audience harness is inserted after ``python``."""
    args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)) for a in hook.get("args", [])]
    uv = _uv() or hook["command"]
    if harness_home is not None:
        i = _python_index(args)
        script = args[i + 1]
        args = args[: i + 1] + [str(HARNESS), str(harness_home), script, *args[i + 2:]]
    return [uv, *args]


def _is_nx_entry(argv_tail: list[str]) -> bool:
    return bool(argv_tail) and (argv_tail[0] in ("launch/nx_launch.py", "hooks/scripts/nx_hook_shim.py"))


def _common(fx: Fixture, event: str, **extra: Any) -> dict:
    return {"session_id": f"leg-{fx.name}", "cwd": str(fx.repo), "hook_event_name": event,
            "transcript_path": str(fx.repo.parent / f"transcript-{fx.name}.jsonl"), **extra}


def _bash(fx: Fixture, command: str, agent_type: str = "general-purpose") -> dict:
    return _common(fx, "PreToolUse", tool_name="Bash", tool_input={"command": command},
                   agent_id=f"aleg-{fx.name}-0000000000", agent_type=agent_type)


def _no_put_transcript(path: Path) -> None:
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "do it"}}) + "\n"
                    + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}) + "\n")


def build_probes(plugin: Path, manifest: dict, hooks_json: dict, fixtures: dict[str, Fixture], postures: list[str],
                 only: str, account_mode: str, real_flagged: bool) -> list[Probe]:
    probes: list[Probe] = []
    regs = {r["key"]: r for r in am.registrations(hooks_json)}
    for r in manifest["registrations"]:
        reg = regs[r["key"]]
        tail = reg["argv"]
        nx_entry = _is_nx_entry(tail)
        is_guard = tail[-1].endswith("subagent_git_write_requires_orchestrator.py")
        for fx in fixtures.values():
            if fx.name == "plain-dirty" and not is_guard:
                continue  # the dirty tree exists for the guard's git-state rows alone
            if fx.name in BEADS_FIXTURES and not _is_compaction(reg):
                continue  # the beads directory exists for the compaction text's data gate alone
            if account_mode == "harness":
                use_harness = True
            elif account_mode == "real":
                use_harness = fx.flagged_home
            else:
                use_harness = real_flagged or fx.flagged_home
            for posture in postures:
                if posture == "P0" and (not nx_entry or fx.name != "plain"):
                    continue
                for scenario in _scenarios(reg, fx):
                    pid = f"{posture}/{fx.name}/{r['key']}/{scenario['name']}"
                    if only and only not in pid:
                        continue
                    extra = {"must": tuple(scenario.get("must", ())), "forbid": tuple(scenario.get("forbid", ())),
                             "private_cfg": bool(scenario.get("private_cfg", False))}
                    if reg["hook"].get("type") == "mcp_tool":
                        probes.append(Probe(pid, r["key"], fx.name, posture, "module", [], scenario["payload"], scenario.get("expect"),
                                            module_driver=SUBAGENT_START_DRIVER, **extra))
                        continue
                    argv = entry_argv(plugin, reg["hook"], harness_home=fx.home if (use_harness and not nx_entry) else None)
                    probes.append(Probe(pid, r["key"], fx.name, posture, "nx" if nx_entry else "script", argv, scenario["payload"],
                                        scenario.get("expect"), group=scenario.get("group", ""), seq=scenario.get("seq", 0), **extra))
    return probes


def _is_compaction(reg: dict) -> bool:
    """PreCompact, PostCompact and the compact SessionStart inject: the events whose text S2-2 gates on the project's beads."""
    return reg["event"] in ("PreCompact", "PostCompact") or (reg["event"] == "SessionStart" and reg["argv"][-1] == "inject")


def _scenarios(reg: dict, fx: Fixture) -> list[dict]:
    """The payloads for one registration under one fixture. The shapes are the ones Claude Code sends."""
    event, tail = reg["event"], reg["argv"]
    key = reg["key"]
    user = fx.name in USER_FIXTURES
    out: list[dict] = []
    if event == "PreToolUse" and tail[-1].endswith("subagent_git_write_requires_orchestrator.py"):
        for cmd, u, m in GUARD_ROWS:
            out.append({"name": cmd, "payload": _bash(fx, cmd), "expect": u if user else m})
        if fx.name == "plain-dirty":
            for cmd in DIRTY_ROWS:
                out.append({"name": cmd, "payload": _bash(fx, cmd), "expect": "deny"})
        elif fx.name == "plain":
            for cmd in DIRTY_ROWS:
                out.append({"name": cmd, "payload": _bash(fx, cmd), "expect": "pass"})
        else:
            for cmd in DIRTY_ROWS:
                out.append({"name": cmd, "payload": _bash(fx, cmd), "expect": "deny"})
        return out
    if event == "PreToolUse" and tail[-1].endswith("credential_print_guard.py"):
        # U7b: the wrapper the redirect names is the nexus checkout's; every other repository is told to ask its operator.
        out = []
        for cmd, exp in CREDENTIAL_ROWS:
            sc: dict = {"name": cmd, "payload": _bash(fx, cmd), "expect": exp}
            if exp == "deny":
                sc["must"] = ("claude_credentials.py status",) if fx.name == "marked" else ("Ask your operator",)
                sc["forbid"] = () if fx.name == "marked" else ("claude_credentials", "Hellblazer/nexus")
            out.append(sc)
        return out
    if event == "PreToolUse" and tail[-1].endswith("destructive_action_advice.py"):
        return [{"name": cmd, "payload": _bash(fx, cmd), "expect": what} for cmd, what in ADVICE_ROWS]
    if event in ("PreToolUse", "PermissionRequest") and tail[-1] == "auto-approve":
        # U2b: the six tools are the account flag's alone (the marker does not loosen); every other allowed tool is everyone's
        out = [{"name": "search", "expect": "allow", "payload": _common(
            fx, event, tool_name="mcp__plugin_conexus_nexus__search", tool_input={"query": "x", "project": "p", "title": "t"})}]
        for tool in PROMPTED_FOR_USERS:
            out.append({"name": tool, "expect": "allow" if fx.flagged_home else "none", "payload": _common(
                fx, event, tool_name=f"mcp__plugin_conexus_nexus__{tool}", tool_input={"query": "x", "project": "p", "title": "t"})})
        return out
    if event == "Stop":
        nexus = fx.name == "marked"
        promise = "The suite is still running. When it finishes, I'll read the result and close the bead."
        ci_wait = "Pushed. I'll read the CI result once the runs finish."
        out = []
        for name, text in (("closing promise", promise), ("ci wait", ci_wait)):
            sc = {"name": name, "expect": "advice", "private_cfg": True, "payload": _common(
                fx, "Stop", stop_hook_active=False, background_tasks=[], session_crons=[], last_assistant_message=text)}
            if name == "ci wait":  # U6b: the CI wording is a nexus checkout's alone
                sc["must" if nexus else "forbid"] = ("nx ci wait",) if nexus else CI_TEXT
            elif not nexus:
                sc["forbid"] = CI_TEXT
            out.append(sc)
        return out
    if event == "UserPromptSubmit":
        return [{"name": "prompt", "payload": _common(fx, "UserPromptSubmit", prompt="what is the status"), "expect": None}]
    if event == "PreCompact":
        return [{"name": "pre", "group": f"compact-{fx.name}", "seq": 0, "expect": None,
                 "payload": _common(fx, "PreCompact", trigger="manual", custom_instructions="")}]
    if event == "PostCompact":
        return [{"name": "post", "payload": _common(fx, "PostCompact", trigger="manual", compact_summary="a summary"), "expect": None}]
    if event == "SessionEnd":
        return [{"name": "end", "payload": _common(fx, "SessionEnd", reason="clear"), "expect": None}]
    if event == "SubagentStop":
        outs = []
        for agent_type in ("conexus:developer", "general-purpose"):
            # U5a: a conexus agent with no T1 put is nudged for everyone. U5b: any other type only for the maintainer.
            nudged = agent_type.startswith("conexus:") or fx.name not in USER_FIXTURES
            outs.append({"name": agent_type, "payload": _common(
                fx, "SubagentStop", agent_id=f"aleg-{fx.name}-stop", agent_type=agent_type, stop_hook_active=False,
                agent_transcript_path=str(fx.repo.parent / f"agent-{fx.name}-{agent_type.replace(':', '_')}.jsonl")),
                "expect": "advice" if nudged else "none"})
        return outs
    if event == "SubagentStart":
        starts = [{"name": at, "payload": _common(fx, "SubagentStart", agent_id=f"aleg-{fx.name}-start", agent_type=at, prompt_id="p1"), "expect": None}
                  for at in ("conexus:developer", "general-purpose", "Explore")]
        # The Phase Boundary Gate block is task-keyed; its maintainer-only incident clause must be reachable by the presence check.
        starts.append({"name": "phase close", "expect": None, "payload": _common(
            fx, "SubagentStart", agent_id=f"aleg-{fx.name}-phase", agent_type="conexus:developer", prompt_id="p2",
            task="close the phase review gate for this phase")})
        return starts
    if event == "SessionStart":
        source = "compact" if tail[-1] == "inject" else "startup"
        first = {"name": source, "payload": _common(fx, "SessionStart", source=source, model="m"), "expect": None,
                 "group": f"compact-{fx.name}" if tail[-1] == "inject" else "", "seq": 1}
        if tail[-1] != "inject":
            return [first]
        # S2-2: a restored checkpoint and a compact start with none; the board and beads are named only where the project has them
        beads = fx.name in BEADS_FIXTURES
        maint = fx.name in ("marked", "flagged")
        # the restored text carries the checkpoint's own body (it says "board" in an error line): assert the intro's wording
        first["must"] = ("git or beads",) if beads else ()
        first["forbid"] = (() if maint else ("threads board", "a board thread")) + (() if beads else ("or beads", "and beads"))
        none = {"name": "compact, no checkpoint", "expect": None, "payload": _common(
            fx, "SessionStart", source="compact", model="m", session_id=f"leg-{fx.name}-nockpt")}
        none["must"] = (("bd list --status=in_progress",) if beads else ()) + (("threads board entry",) if maint else ())
        none["forbid"] = (() if maint else ("board", "threads")) + (() if beads else ("bd list", "beads"))
        return [first, none]
    return [{"name": "default", "payload": _common(fx, event), "expect": None}]


# --- running ---------------------------------------------------------------------------------------------


def run_probe(probe: Probe, env: dict[str, str], cwd: Path, timeout: float) -> Outcome:
    payload = probe.stdin_raw if probe.stdin_raw is not None else json.dumps(probe.payload)
    if probe.kind == "module":
        argv = [MODULE_PYTHON, "-c", probe.module_driver]
    else:
        argv = probe.argv
    # The compact and SubagentStop probes read a transcript the hook finds by path.
    if probe.payload and isinstance(probe.payload.get("agent_transcript_path"), str):
        _no_put_transcript(Path(probe.payload["agent_transcript_path"]))
    if probe.payload and isinstance(probe.payload.get("transcript_path"), str) and not Path(probe.payload["transcript_path"]).exists():
        _no_put_transcript(Path(probe.payload["transcript_path"]))
    private: Path | None = None
    if probe.private_cfg:
        private = Path(tempfile.mkdtemp(prefix="plugin-behaviour-cfg.", dir=env.get("TMPDIR") or None))
        env = {**env, "NEXUS_CONFIG_DIR": str(private)}
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, cwd=str(cwd), check=False)
        rc, out, err, note = proc.returncode, proc.stdout, proc.stderr, ""
    except subprocess.TimeoutExpired:
        rc, out, err, note = 124, "", "", f"timed out after {timeout}s"
    leaked: tuple[str, ...] = ()
    if private is not None:
        leaked = tuple(sorted(str(q.relative_to(private)) for q in private.rglob("*")))
        shutil.rmtree(private, ignore_errors=True)
    decision, text = classify(out)
    return Outcome(probe, rc, out, err, decision, text, note, leaked)


def run_all(probes: list[Probe], envs: dict[tuple[str, str], dict[str, str]], fixtures: dict[str, Fixture], jobs: int,
            timeout: float) -> list[Outcome]:
    groups: dict[str, list[Probe]] = {}
    order: list[list[Probe]] = []
    for p in probes:
        if p.group:
            if p.group not in groups:
                groups[p.group] = []
                order.append(groups[p.group])
            groups[p.group].append(p)
        else:
            order.append([p])

    def work(batch: list[Probe]) -> list[Outcome]:
        return [run_probe(p, envs[(p.posture, p.fixture)], fixtures[p.fixture].repo, timeout) for p in sorted(batch, key=lambda q: q.seq)]

    outcomes: list[Outcome] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for res in pool.map(work, order):
            outcomes.extend(res)
    return outcomes


def evaluate(outcomes: list[Outcome], manifest: dict, repo: Path) -> tuple[list[dict], list[str], dict[str, str], dict[str, int]]:
    """``(lexicon and deny findings that an allowlist may cover, hard failures, per-fixture text, counts)``."""
    findings: list[dict] = []
    hard: list[str] = []
    texts: dict[str, str] = {f: "" for f in FIXTURES}
    counts = {"payloads": 0, "silent": 0, "fired": 0}
    for o in outcomes:
        p = o.probe
        counts["payloads"] += 1
        counts["silent" if o.decision == "none" else "fired"] += 1
        if o.rc != 0:
            hard.append(f"{p.id}: exited {o.rc} ({o.note or o.stderr.strip()[:160]})")
            continue
        if p.posture == "P0":
            if o.stdout.strip():
                hard.append(f"{p.id}: P0 (no nx) printed on standard output: {o.stdout.strip()[:120]!r}")
            elif not o.stderr.strip():
                # The launcher and the shim each say on standard error that the CLI was skipped. No note means the
                # program may have been found and run, and a posture with nx present is not P0.
                hard.append(f"{p.id}: P0 produced no 'skipped' note on standard error, so nx may not have been absent")
            continue
        texts[p.fixture] += "\n" + o.text
        if p.expect is not None:
            if p.expect == "pass" and o.decision in ("deny", "block"):
                hard.append(f"{p.id}: expected the call to pass, got {o.decision}")
            elif p.expect in ("deny", "block", "advice", "none", "allow") and o.decision != p.expect:
                hard.append(f"{p.id}: expected {p.expect}, got {o.decision}")
        elif p.fixture in USER_FIXTURES and o.decision in ("deny", "block"):
            findings.append({"reg_key": p.reg_key, "hit": f"<{o.decision}>", "probe": p.id})
        low = o.text.lower()
        for word in p.must:
            if word.lower() not in low:
                hard.append(f"{p.id}: the output does not carry {word!r}")
        for word in p.forbid:
            if word.lower() in low:
                hard.append(f"{p.id}: the output carries {word!r}")
        if o.leaked:
            hard.append(f"{p.id}: the hook left {list(o.leaked)} in a config directory that did not exist (a Stop hook must not create one)")
        if p.fixture in USER_FIXTURES:
            for hit in sorted(set(lexicon_hits(o.text, beads=p.fixture in BEADS_FIXTURES))):
                findings.append({"reg_key": p.reg_key, "hit": hit, "probe": p.id})
    return findings, hard, texts, counts


# --- main -----------------------------------------------------------------------------------------------


def run_leg(args: argparse.Namespace) -> int:
    plugin = Path(args.plugin).resolve()
    manifest_path = plugin / "hooks" / "audience.yaml"
    if not manifest_path.is_file():
        print(f"PLUGIN BEHAVIOUR LEG UNVERIFIED -- {plugin} has no hooks/audience.yaml; a plugin with hooks and no manifest is not a pass")
        return 2
    try:
        manifest = am.load_manifest(manifest_path)
    except ImportError:
        print("PLUGIN BEHAVIOUR LEG UNVERIFIED -- PyYAML is not importable; run this leg under the project's interpreter (uv run python ...)")
        return 2
    hooks_json = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    if _uv() is None:
        print("PLUGIN BEHAVIOUR LEG UNVERIFIED -- uv is not on PATH; the hooks' own argv cannot run")
        return 2
    regs = am.registrations(hooks_json)
    declared = {r["key"] for r in manifest["registrations"]}
    missing = [r["key"] for r in regs if r["key"] not in declared]
    if missing or len(regs) < am.MIN_REGISTRATIONS:
        print(f"PLUGIN BEHAVIOUR LEG FAILED -- {len(regs)} registrations, {len(missing)} with no manifest entry: {missing[:3]}")
        return 1
    postures = ["P0", "P2"] if args.posture == "both" else [args.posture]
    allow_text = Path(args.allowlist).read_text(encoding="utf-8") if Path(args.allowlist).is_file() else ""
    entries = parse_allowlist(allow_text)
    problems = allowlist_problems(entries, manifest)
    if problems:
        print("PLUGIN BEHAVIOUR LEG FAILED -- the allowlist:\n  " + "\n  ".join(problems))
        return 1

    real_flagged = _account_flagged(plugin)
    root = scratch_root()
    if root is None:
        print("PLUGIN BEHAVIOUR LEG UNVERIFIED -- every candidate scratch root sits inside a git repository; set TMPDIR elsewhere", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="plugin-behaviour.", dir=root)).resolve()
    try:
        fixtures = make_fixtures(work)
        shim_dir = work / "shims"
        nx_python = installed_python() if args.nx == "installed" else sys.executable
        if "P2" in postures:
            if nx_python is None:
                print("PLUGIN BEHAVIOUR LEG UNVERIFIED -- --nx installed: no nx-hook on PATH to take an interpreter from")
                return 2
            write_shims(shim_dir, nx_python)
            global MODULE_PYTHON
            MODULE_PYTHON = nx_python
        uv_env = real_uv_env()
        envs = {(po, name): base_env(work, plugin, fx, po, shim_dir, uv_env) for po in postures for name, fx in fixtures.items()}
        mode = {"auto": "real argv" if not real_flagged else "audience harness (the real account carries the flag)",
                "real": "real argv", "harness": "audience harness"}[args.account]
        print(f"== plugin behaviour leg: {plugin} ==")
        print(f"   account: {mode}; postures: {','.join(postures)}; nx: {args.nx if 'P2' in postures else 'none'}; manifest registrations: {len(declared)}")
        probes = build_probes(plugin, manifest, hooks_json, fixtures, postures, args.only or "", args.account, real_flagged)
        if not probes:
            print("PLUGIN BEHAVIOUR LEG FAILED -- no probe was built; a pass over nothing is not a pass")
            return 1
        outcomes = run_all(probes, envs, fixtures, args.jobs, args.timeout)
        findings, hard, texts, counts = evaluate(outcomes, manifest, REPO)
        failures, allowed = apply_allowlist(findings, entries, manifest)
        for e in entries:
            if e.used == 0 and not args.only:
                hard.append(f"stale allowlist entry: {e.row} / {e.hit!r} matched nothing")
        presence: list[str] = []
        examined = 0
        if "P2" in postures and not args.only:
            presence, examined = presence_problems(manifest, REPO, texts)
        for post in postures:
            ps = [o for o in outcomes if o.probe.posture == post]
            keys = {o.probe.reg_key for o in ps}
            silent = sum(1 for o in ps if o.decision == "none")
            print(f"   {post}: {len(keys)} registrations driven, {len(ps)} payloads, {silent} silent, {len(ps) - silent} fired")
        if args.verbose:
            for o in outcomes:
                print(f"--- {o.probe.id}: rc {o.rc}, {o.decision}\n{o.text[:4000]}\n[stderr] {o.stderr.strip()[:200]}")
        print(f"   allowlisted outputs: {len(allowed)}; constants whose presence was checked: {examined}")
        by_decision: dict[str, int] = {}
        for o in outcomes:
            by_decision[o.decision] = by_decision.get(o.decision, 0) + 1
        print("   outputs by decision: " + ", ".join(f"{k} {v}" for k, v in sorted(by_decision.items())))
        bad = hard + presence + [f"{f['probe']}: {f['hit']} (registration {f['reg_key']})" for f in failures]
        if "P2" in postures and not args.only:
            driven = {o.probe.reg_key for o in outcomes if o.probe.posture == "P2"}
            for k in sorted(declared - driven):
                bad.append(f"registration {k} was never driven in P2")
        if bad:
            print("PLUGIN BEHAVIOUR LEG FAILED -- " + str(len(bad)) + " problem(s):\n  " + "\n  ".join(bad[:60]))
            return 1
        print(f"PLUGIN BEHAVIOUR LEG PASSED -- {len(outcomes)} payloads, {len(allowed)} allowlisted")
        return 0
    finally:
        if args.keep:
            print(f"   kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--plugin", required=True)
    run.add_argument("--posture", choices=("P0", "P2", "both"), default="both")
    run.add_argument("--nx", choices=("checkout", "installed"), default="checkout")
    run.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    run.add_argument("--account", choices=("auto", "real", "harness"), default="auto")
    run.add_argument("--only", default="")
    run.add_argument("--jobs", type=int, default=int(os.environ.get("NX_GATE_JOBS", "4")))
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--keep", action="store_true")
    run.add_argument("--verbose", action="store_true", help="print each selected probe's decision and the start of its output")
    ns = ap.parse_args(argv)
    return run_leg(ns)


if __name__ == "__main__":
    sys.exit(main())
