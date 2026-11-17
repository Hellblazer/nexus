#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the census and lint meta-tests a diff can turn red, before the push.

A meta-test checks the repo's shape rather than a behaviour: it scans files
(every module under src/nexus, every test, every doc) and fails on a new one
that breaks a rule. 42% of develop's failed CI runs from 10-19 to 11-17 failed
only on these (T2 nexus/observability-lookback-2026-11-17, item 1), mostly
because the whole lint bucket is a lab run that a push skipped. This script
picks the few a diff reaches and runs them in seconds.

The meta-tests (``known``):

* every test file under tests/ that carries ``pytest.mark.lint`` or
  ``pytest.mark.census`` (a census that stays in the default loop);
* the session-items censuses conftest names in
  ``_SESSION_ITEMS_CENSUS_TEST_NODEIDS``: they judge the tests collected in
  their own invocation, so they run together with the changed test files;
* the engine's Java tests that walk source trees (``Files.walk``, ``list``,
  ``find`` or ``newDirectoryStream``), except ``*IntegrationTest``.

Each Python meta-test's surface is derived, never declared: ``build`` runs
them all under ``tests/_surface_trace.py`` and writes what each read to
``tests/meta_surfaces.json``, with the hash of the test file. A changed path
selects a test when the test opened or statted it, listed its directory (for a
new directory, its first ancestor that exists at the base), ran a subprocess
that enumerates a subtree holding it, or imports it directly (a ``src/nexus``
or ``tests`` module). A test runs whatever the diff when the manifest does not
hold it, its file changed since the build, or it ran a subprocess whose reads
the trace cannot see. Every such test is listed by reason, so a new meta-test
cannot fall out unseen. A Java meta-test's surface is ``service/src/``.

Usage::

    scripts/meta_tests.py select --base origin/develop      # what would run, and why
    scripts/meta_tests.py run --base origin/develop         # run the Python ones
    scripts/meta_tests.py build                             # rewrite the manifest (a lab run)
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "tests/meta_surfaces.json"
MARKS = ("lint", "census")
CONFTEST = "tests/conftest.py"
SESSION_ITEMS_NAME = "_SESSION_ITEMS_CENSUS_TEST_NODEIDS"
JAVA_TEST_ROOT = "service/src/test/java"
JAVA_WALK = re.compile(r"Files\.(walk|list|find|newDirectoryStream)\(")
JAVA_SURFACE = "service/src/"
PYTEST = ["uv", "run", "pytest", "-q", "-rf", "-p", "no:randomly", "--color=no"]
#: Meta-tests read files; none needs the engine, so no gate jar is built or checked for them.
ENV = {"NX_TEST_T2_SUBSTRATE": "none"}
#: The meta-tests in a selected file: a file may hold behaviour tests beside its census.
SELECT_MARK = "(lint or census) and not integration and not slow"
#: The session-items guard judges what its own invocation collected, so the changed test
#: files run with it unfiltered (a -m that deselects reads as a partial view and skips it).
GUARD_MARK = "not integration and not slow"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── discovery ────────────────────────────────────────────────────────────────


def _marks_in(tree: ast.AST) -> set[str]:
    """The pytest marks among MARKS this module applies anywhere (pytest.mark.X)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in MARKS
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            found.add(node.attr)
    return found


def session_items_files(repo: Path) -> set[str]:
    tree = ast.parse((repo / CONFTEST).read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == SESSION_ITEMS_NAME for t in targets):
            value = getattr(node, "value", None)
            if value is None:
                break
            ids = [c.value for c in ast.walk(value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            return {i.split("::", 1)[0] for i in ids}
    raise SystemExit(f"meta_tests: {CONFTEST} no longer defines {SESSION_ITEMS_NAME}; update this script")


def python_meta_tests(repo: Path) -> dict[str, set[str]]:
    """Test file -> its kinds ('lint', 'census', 'session-items')."""
    out: dict[str, set[str]] = {}
    for path in sorted((repo / "tests").rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if "__pycache__" in rel or not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if not any(f"mark.{m}" in text for m in MARKS):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                marks = _marks_in(ast.parse(text, filename=rel))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if marks:
            out[rel] = marks
    for rel in session_items_files(repo):
        out.setdefault(rel, set()).add("session-items")
    return out


def java_meta_tests(repo: Path) -> list[str]:
    out = []
    root = repo / JAVA_TEST_ROOT
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*Test.java")):
        if path.name.endswith("IntegrationTest.java"):
            continue
        if JAVA_WALK.search(path.read_text(errors="replace")):
            out.append(path.relative_to(repo).as_posix())
    return out


# ── imports (the half of a surface the trace leaves out) ────────────────────


def module_files(repo: Path, test: str) -> set[str]:
    """Repo files the test file imports directly: src/nexus and tests modules, with package __init__s."""
    try:
        tree = ast.parse((repo / test).read_text())
    except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    out: set[str] = set()
    for name in names:
        top = name.split(".", 1)[0]
        if top == "nexus":
            base = repo / "src"
        elif top == "tests":
            base = repo
        else:
            continue
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            stem = base.joinpath(*parts[:i])
            for cand in (stem.with_suffix(".py"), stem / "__init__.py"):
                if cand.is_file():
                    out.add(cand.relative_to(repo).as_posix())
    return out


# ── selection ────────────────────────────────────────────────────────────────


@dataclass
class Selection:
    known: dict[str, set[str]]
    java_known: list[str]
    selected: dict[str, str] = field(default_factory=dict)  # test file -> reason
    always: dict[str, str] = field(default_factory=dict)  # test file -> why it runs whatever the diff
    java: dict[str, str] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)


def changed_paths(repo: Path, base: str) -> list[str]:
    names = set(git(repo, "diff", "--no-renames", "--name-only", f"{base}...HEAD").split())
    names |= set(git(repo, "diff", "--no-renames", "--name-only", "HEAD").split())
    names |= set(git(repo, "ls-files", "--others", "--exclude-standard").split())
    return sorted(names)


def base_dirs(repo: Path, base: str) -> set[str]:
    return {""} | {d for d in git(repo, "ls-tree", "-r", "-d", "--name-only", base).splitlines() if d}


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _listed_hit(path: str, listed: set[str], existing: set[str]) -> bool:
    for anc in _ancestors(path):
        if anc in existing:
            return anc in listed
    return "" in listed


def _prefix_hit(path: str, prefixes: list[str]) -> bool:
    return any(p == "" or path == p or path.startswith(p + "/") for p in prefixes)


def select(repo: Path, changed: list[str], base: str, manifest: dict | None = None) -> Selection:
    if manifest is None:
        mpath = repo / MANIFEST
        manifest = json.loads(mpath.read_text()) if mpath.is_file() else {}
    surfaces: dict = (manifest or {}).get("tests", {})
    known = python_meta_tests(repo)
    sel = Selection(known=known, java_known=java_meta_tests(repo), changed=changed)
    existing = base_dirs(repo, base) if changed else {""}
    changed_tests = [p for p in changed if p.startswith("tests/") and p.endswith(".py")]

    for test, kinds in known.items():
        if "session-items" in kinds:
            if changed_tests:
                sel.selected[test] = f"judges the tests collected with it; runs with the {len(changed_tests)} changed test file(s)"
            continue
        entry = surfaces.get(test)
        path = repo / test
        if entry is None:
            sel.always[test] = "not in the manifest (new since the build): surface unknown"
            continue
        if not path.is_file() or entry.get("sha256") != sha256(path):
            sel.always[test] = "its file changed since the manifest was built: surface may have moved"
            continue
        if entry.get("opaque"):
            sel.always[test] = "runs a subprocess whose reads the trace cannot see: " + entry["opaque"][0]
            continue
        hit = _hit(test, entry, changed, existing, module_files(repo, test))
        if hit:
            sel.selected[test] = hit
    if any(p.startswith(JAVA_SURFACE) for p in changed):
        first = next(p for p in changed if p.startswith(JAVA_SURFACE))
        for test in sel.java_known:
            sel.java[test] = f"walks the engine source tree; {first} changed"
    return sel


def _hit(test: str, entry: dict, changed: list[str], existing: set[str], imports: set[str]) -> str | None:
    opened = set(entry.get("opened", ())) | set(entry.get("statted", ()))
    listed = set(entry.get("listed", ()))
    prefixes = list(entry.get("prefixes", ()))
    for p in changed:
        if p == test:
            return "the test file itself changed"
        if p in opened:
            return f"reads {p}"
        if p in imports:
            return f"imports {p}"
        if _prefix_hit(p, prefixes):
            return f"enumerates a subtree holding {p}"
        if _listed_hit(p, listed, existing):
            return f"lists the directory of {p}"
    return None


def java_class(test: str) -> str:
    return Path(test).stem


def report(sel: Selection, out=None) -> None:
    out = out or sys.stdout
    kinds = [k for v in sel.known.values() for k in v]
    print(
        f"meta-tests known: {len(sel.known)} Python ({kinds.count('lint')} lint, {kinds.count('census')} census,"
        f" {kinds.count('session-items')} session-items), {len(sel.java_known)} Java; changed paths: {len(sel.changed)}",
        file=out,
    )
    print(f"selected by the diff: {len(sel.selected)}", file=out)
    for test, why in sorted(sel.selected.items()):
        print(f"  {test}: {why}", file=out)
    print(f"run whatever the diff: {len(sel.always)}", file=out)
    for test, why in sorted(sel.always.items()):
        print(f"  {test}: {why}", file=out)
    if sel.java:
        print(f"Java meta-tests selected: {len(sel.java)} (a heavy engine run; see the command below)", file=out)


def java_command(sel: Selection) -> str | None:
    if not sel.java:
        return None
    classes = ",".join(sorted(java_class(t) for t in sel.java))
    return (
        "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dnexus.ddl.suite=main"
        f" -Dsurefire.failIfNoSpecifiedTests=false -Dtest={shlex.quote(classes)}"
    )


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_select(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    changed = sorted(sys.stdin.read().split()) if args.names_from_stdin else changed_paths(repo, args.base)
    t0 = time.monotonic()
    sel = select(repo, changed, args.base)
    if args.json:
        json.dump(
            {"selected": sel.selected, "always": sel.always, "java": sel.java, "changed": sel.changed,
             "known": len(sel.known), "java_known": len(sel.java_known)},
            sys.stdout, indent=1, sort_keys=True,
        )
        print()
    else:
        report(sel)
        print(f"selection took {time.monotonic() - t0:.1f}s")
        cmd = java_command(sel)
        if cmd:
            print(cmd)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    t0 = time.monotonic()
    changed = changed_paths(repo, args.base)
    sel = select(repo, changed, args.base)
    t_select = time.monotonic() - t0
    report(sel)
    picked = sorted(set(sel.selected) | set(sel.always))
    guards = [f for f in picked if "session-items" in sel.known.get(f, ())]
    files = [f for f in picked if f not in guards and (repo / f).is_file()]
    changed_tests = sorted(p for p in changed if p.startswith("tests/") and Path(p).name.startswith("test_")
                           and p.endswith(".py") and (repo / p).is_file())
    print(f"selection took {t_select:.1f}s")
    rc = 0
    runs = []
    if files:
        runs.append(("meta-tests", [*PYTEST, "-o", "addopts=", "-m", SELECT_MARK, *files]))
    if guards:
        runs.append(("session-items guard with the changed test files",
                     [*PYTEST, "-o", "addopts=", "-m", GUARD_MARK, *guards, *changed_tests]))
    if not runs:
        print("no Python meta-test selected")
    for label, argv in runs:
        print("$ " + shlex.join(argv), flush=True)
        t1 = time.monotonic()
        code = subprocess.run(argv, cwd=repo, env={**os.environ, **ENV}).returncode
        print(f"{label} ran in {time.monotonic() - t1:.1f}s (exit {code})")
        rc = rc or code
    cmd = java_command(sel)
    if cmd:
        print("Java meta-tests to run (heavy, not run here; the engine class's full suite also covers them):")
        print(cmd)
    return rc


def cmd_build(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    known = python_meta_tests(repo)
    files = sorted(f for f, k in known.items() if "session-items" not in k)
    with tempfile.TemporaryDirectory(prefix="meta-surface-") as out:
        argv = [*PYTEST, "-p", "tests._surface_trace", "-o", "addopts=", "-m", SELECT_MARK, *args.pytest_args, *files]
        # -p loads the plugin before pytest puts the rootdir on sys.path.
        path = os.pathsep.join(p for p in (str(repo), os.environ.get("PYTHONPATH", "")) if p)
        env = dict(os.environ, **ENV, NX_SURFACE_TRACE_OUT=out, PYTHONPATH=path)
        print("$ " + shlex.join(argv), flush=True)
        rc = subprocess.run(argv, cwd=repo, env=env).returncode
        merged: dict[str, dict[str, set[str]]] = {}
        for frag in Path(out).glob("trace-*.json"):
            for test, kinds in json.loads(frag.read_text()).items():
                for kind, items in kinds.items():
                    merged.setdefault(test, {}).setdefault(kind, set()).update(items)
    tests = {}
    for test in files:
        kinds = merged.get(test, {})
        listed = sorted(kinds.get("listed", ()))
        listed_set = set(listed)
        # A file read in a directory the test also lists adds nothing: the listing selects it.
        opened = sorted(p for p in kinds.get("opened", ()) if p.rsplit("/", 1)[0] not in listed_set)
        statted = sorted(p for p in kinds.get("statted", ()) if p.rsplit("/", 1)[0] not in listed_set and p not in opened)
        prefixes = sorted(_minimal_prefixes(kinds.get("prefixes", ())))
        tests[test] = {
            "sha256": sha256(repo / test),
            "opened": opened, "statted": statted, "listed": listed, "prefixes": prefixes,
            "opaque": sorted(kinds.get("opaque", ())),
        }
    manifest = {
        "about": "Generated by scripts/meta_tests.py build; do not edit. What each meta-test read, per file.",
        "built_from": git(repo, "rev-parse", "HEAD").strip(),
        "tests": tests,
    }
    (repo / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    traced = sum(1 for t in tests.values() if any(t[k] for k in ("opened", "statted", "listed", "prefixes", "opaque")))
    print(f"wrote {MANIFEST}: {len(tests)} meta-tests, {traced} with a recorded surface (pytest exit {rc})")
    return 0 if rc in (0, 1) else rc


def _minimal_prefixes(prefixes) -> set[str]:
    keep: set[str] = set()
    for p in sorted(set(prefixes), key=len):
        if not _prefix_hit(p, list(keep)):
            keep.add(p)
    return keep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("select", "run"):
        p = sub.add_parser(name)
        p.add_argument("--base", default="origin/develop")
        p.add_argument("--repo", default=".")
        if name == "select":
            p.add_argument("--json", action="store_true")
            p.add_argument("--names-from-stdin", action="store_true",
                           help="read the changed paths from stdin instead of git (a replay)")
    b = sub.add_parser("build")
    b.add_argument("--repo", default=".")
    b.add_argument("pytest_args", nargs="*", help="extra pytest arguments, e.g. -n auto")
    args = parser.parse_args(argv)
    return {"select": cmd_select, "run": cmd_run, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
