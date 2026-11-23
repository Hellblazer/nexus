#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pick the test files a change can affect, so a lane runs them in minutes (RDR-258 (f)).

One entry point over three selectors that already exist, plus the two rules they lack::

    pytest -n auto $(scripts/select_affected_tests.py --base origin/develop)
    scripts/select_affected_tests.py --range <before>..<after> --explain
    git diff --name-only A B | scripts/select_affected_tests.py --files-from -

A test file is selected when ANY of these holds (over-selecting is fine, under-selecting is the failure):

1. **Import closure.** It imports a changed ``src/nexus`` module (or a changed script, conexus module or
   tests/ helper) directly or through any chain of imports. The graph is built with ``ast`` over
   ``src/nexus``, ``tests``, ``scripts`` and ``conexus``: ``import``, ``from .. import``, relative imports,
   function-local imports, the parent packages an import executes, and dotted-module strings
   (``patch("nexus.x.y")``, ``-m nexus.x``, ``pytest_plugins``). A test that spawns ``nx`` depends on the
   console-script entry modules in pyproject.toml.
2. **File reads.** It names a changed non-Python file (toml, ledger, doc, workflow yml, fixture, shell script)
   by path, by basename, by a pathlib chain (``ROOT / "docs" / "x.md"``), by a directory literal that holds it
   or by a glob that matches it. Only code counts: a comment or docstring names nothing. A non-test module
   that names the file is treated as changed (so its importers are selected) only when the name is a whole
   string or a joined path the code can open; a file named inside a sentence (an error message) selects the
   test file that says it and nothing more. A glob must pick files by name
   (``docs/*.md``, ``rdr-*.md``); a bare extension glob (``*.py``) counts only in a test that walks the tree
   (``rglob``, ``**``), which is a lint over every file of that type; a regex is not a glob.
3. **Meta-tests.** ``scripts/meta_tests.py``'s selection (the lint and census tests the diff reaches), and
   ``scripts/select_docs_reader_tests.py``'s (the tests that read ``docs/``) when a docs path changed.
4. **Run-everything.** A change to ``tests/conftest.py``, ``pyproject.toml`` or ``uv.lock``, or to a helper that
   a conftest imports (so every test loads it), prints ``tests`` and says why on stderr. A nested conftest
   selects the tests below its directory. This narrows the blunt "any ``tests/_*.py`` selects everything" to the
   helpers a conftest reaches: the rest go by the import closure above, which is exact for imports.
5. **service/.** A change under ``service/`` prints the ``scripts/mvnw-leased.sh`` line for the touched engine
   test classes (stderr) and selects the Python tests and modules that name a ``/v1/...`` wire route the
   changed Java files serve. With no route to derive it says so; the engine classes are the real check.

Selected paths go to stdout, one per line; every note goes to stderr, so ``pytest $(...)`` is clean. An empty
stdout means nothing was selected (do not run a bare ``pytest``, which runs everything). The index is cached
per file (path, size, mtime) in ``.pytest_cache/``. pytest's default ``-m`` deselects integration, slow and
lint tests: a selected file runs only its default-marker tests, and the lint and census tests need the
``scripts/meta_tests.py run`` invocation.

Stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import warnings
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SCAN_ROOTS = ("src/nexus", "tests", "scripts", "conexus")
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git", "output", ".pytest_cache", "fixtures_big"}
CACHE_REL = ".pytest_cache/select_affected_index.json"
CACHE_VERSION = 9
#: Paths whose change selects every test.
RUN_EVERYTHING = ("tests/conftest.py", "pyproject.toml", "uv.lock", "pytest.ini", "tox.ini", "setup.cfg")
#: A single-segment directory literal selects only for these (a bare ``"docs"`` or ``"src"`` is a collection
#: name or a tmp_path subdirectory in too many tests; docs have their own selector).
SINGLE_DIRS = {"scripts", "conexus", "service", "changelog", ".github", "mcpb", "infra", "third_party"}
DOCS_TRIGGERS = ("docs/", "changelog/")
DOCS_FILES = ("README.md", "CHANGELOG.md", "LICENSE", "LICENSING.md")
#: The recorded per-test seconds, for the time estimate.
DURATIONS = ".test_durations.json"
DEFAULT_WORKERS = 8

_TOKEN_RE = re.compile(
    r"[\w.\-*]+(?:/[\w.\-*]+)+|[\w\-]+\.(?:toml|json|ya?ml|md|sh|sql|xml|txt|py|lock|cfg|ini|tsv|csv|gz|tmpl|j2|html|js|gz)\b"
)
_ROUTE_RE = re.compile(r'"(/v1/[^"\s]*)"')
_DOTTED_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
#: A test file that asks for the engine substrate: the tenant marker or fixtures, or the substrate helpers.
_SUBSTRATE_RE = re.compile(r"t2_tenant|t2_service_env|_engine_substrate|fresh_tenant|substrate_env|ensure_engine")
_WORD_RE = re.compile(r"[A-Za-z][\w-]{1,40}")
#: A file with this many ``add_command`` calls registers a CLI: it imports every command at top level, so a
#: plain import closure through it reaches every test that imports the CLI.
REGISTRAR_MIN = 10
_JOIN_FUNCS = {"joinpath", "join", "Path", "PurePath", "PurePosixPath"}


# ── per-file facts ───────────────────────────────────────────────────────────


def is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith("tests/") and rel.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def module_names(rel: str) -> list[str]:
    """The names a file answers to in an import statement."""
    if not rel.endswith(".py"):
        return []
    parts = rel[:-3].split("/")
    stem = parts[-1]
    names: list[str] = []
    if rel.startswith("src/"):
        mod = parts[1:]
        if stem == "__init__":
            mod = mod[:-1]
        names.append(".".join(mod))
    elif rel.startswith("tests/"):
        mod = parts[:-1] if stem == "__init__" else parts
        names.append(".".join(mod))
        if stem not in ("__init__", "conftest") and not stem.startswith("test_"):
            names.append(stem)
    else:  # scripts/ and conexus/ are put on sys.path, so they import by bare stem
        if stem != "__init__":
            names.append(stem)
            if rel.startswith("scripts/"):
                names.append(".".join(parts))
    return [n for n in names if n]


def _package_of(rel: str) -> str:
    names = module_names(rel)
    dotted = next((n for n in names if "." in n or rel.startswith(("src/", "tests/"))), names[0] if names else "")
    if rel.endswith("/__init__.py"):
        return dotted
    return dotted.rsplit(".", 1)[0] if "." in dotted else ""


def _is_path_literal(text: str) -> bool:
    """True when the whole string is one path-like token (``"docs/x.md"``), not a sentence that mentions one."""
    return len(text) <= 300 and not any(c.isspace() for c in text)


def _tokens_of(text: str) -> list[str]:
    if _is_path_literal(text):
        return [text]
    return _TOKEN_RE.findall(text[:20000])


_GLOB_CHARS_RE = re.compile(r"[\w.\-*/]+")
_BARE_EXT_GLOB_RE = re.compile(r"(?:\*\*/)?\*+\.\w+")
_GLOB_LEAF_RE = re.compile(r"[\w\-*]*\*[\w\-*]*\.\w+$|[\w\-]+\.\w+$|\*$")


def _names_something(glob: str) -> bool:
    """True for a glob that picks out files by name: path characters and ``*`` only (a regex or a URL query
    is not a glob), whose last segment is ``stem*.ext``, ``name.ext`` or a bare ``*`` under a directory, and is
    not only wildcards and an extension. ``*.py`` and ``**/*.md`` name no file in particular: any changed file
    of that type would select every test that globs it."""
    if not _GLOB_CHARS_RE.fullmatch(glob) or "?" in glob:
        return False
    head, _, leaf = glob.rpartition("/")
    if not _GLOB_LEAF_RE.fullmatch(leaf):
        return False
    if leaf == "*":
        return bool(head.replace("*", "").replace("/", ""))
    stem = re.sub(r"\.\w+$", "", leaf)
    return bool(stem.replace("*", "")) or bool(head.replace("*", "").replace("/", ""))


def _clean_token(tok: str) -> str | None:
    t = tok.strip().strip("\"'")
    while t.startswith("./"):
        t = t[2:]
    t = t.lstrip("/").rstrip("/")
    if len(t) < 3 or "://" in t or "{" in t or "%" in t or t.startswith("-"):
        return None
    if "/" in t or "*" in t or "?" in t:
        return t
    if "." in t or t in SINGLE_DIRS:
        return t
    return None


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _runs(parts: list[ast.Constant | None]) -> list[tuple[str, list[int]]]:
    """Joined runs of two or more adjacent string parts, each with the ids of the constants it joins."""
    out: list[tuple[str, list[int]]] = []
    cur: list[ast.Constant] = []
    for p in [*parts, None]:
        if p is not None:
            cur.append(p)
        else:
            if len(cur) >= 2:
                out.append(("/".join(c.value.strip("/") for c in cur), [id(c) for c in cur]))
            cur = []
    return out


def _const_str(node: ast.AST) -> ast.Constant | None:
    return node if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _div_parts(node: ast.AST, acc: list[ast.Constant | None], skip: set[int]) -> None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        skip.add(id(node))
        _div_parts(node.left, acc, skip)
        _div_parts(node.right, acc, skip)
    else:
        acc.append(_const_str(node))


def extract_facts(text: str, rel: str) -> dict:
    """Imports, dotted-module strings and path-ish tokens of one Python file."""
    facts: dict = {"imports": [], "modrefs": [], "tokens": [], "mentions": [], "scans": False, "spawns_nx": False, "words": [], "registrar": {}, "substrate": False}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError, RecursionError):
        return facts
    pkg = _package_of(rel)
    imports: set[str] = set()
    modrefs: set[str] = set()
    tokens: set[str] = set()
    prose: set[str] = set()  # tokens fished out of a sentence (an error message)
    literal_tokens: set[str] = set()  # tokens that are a whole string or a joined path: what the code can open
    doc = _docstring_ids(tree)
    skip: set[int] = set()
    # constants a joined path (``ROOT / "scripts" / "x.py"``) is made of: the run names the file, so a lone
    # "scripts" in it is a path segment, not a literal naming the whole directory
    in_run: set[int] = set()
    words: set[str] = set()
    is_test = rel.startswith("tests/")
    symmod: dict[str, str] = {}
    adds: list[tuple[str, str | None]] = []
    uses_subprocess = "subprocess" in text
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".") if pkg else []
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                mod = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                mod = node.module or ""
            if mod:
                imports.add(mod)
                imports.update(f"{mod}.{a.name}" for a in node.names if a.name != "*")
                for a in node.names:
                    symmod[a.asname or a.name] = mod
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc:
            s = node.value
            if is_test and len(s) <= 300:
                words.update(_WORD_RE.findall(s))
            if s in ("nx", "nx-mcp", "nx-mcp-catalog") and uses_subprocess:
                facts["spawns_nx"] = True
            if _DOTTED_RE.match(s) and s.split(".", 1)[0] in ("nexus", "tests", "conexus"):
                modrefs.add(s)
            literal = _is_path_literal(s)
            for tok in () if id(node) in in_run else _tokens_of(s):
                ct = _clean_token(tok)
                if ct:
                    tokens.add(ct)
                    (literal_tokens if literal else prose).add(ct)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in skip:
            parts: list[ast.Constant | None] = []
            _div_parts(node, parts, skip)
            for run, ids in _runs(parts):
                ct = _clean_token(run)
                if ct:
                    tokens.add(ct)
                    literal_tokens.add(ct)
                    in_run.update(ids)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name == "add_command" and node.args and isinstance(node.args[0], ast.Name):
                kw = next((k.value.value for k in node.keywords if k.arg == "name" and isinstance(k.value, ast.Constant)), None)
                adds.append((node.args[0].id, kw if isinstance(kw, str) else None))
            if name in _JOIN_FUNCS:
                for run, ids in _runs([_const_str(a) for a in node.args]):
                    ct = _clean_token(run)
                    if ct:
                        tokens.add(ct)
                        literal_tokens.add(ct)
                        in_run.update(ids)
    if len(adds) >= REGISTRAR_MIN:
        reg: dict[str, set[str]] = {}
        for sym, cmd in adds:
            mod = symmod.get(sym)
            if mod:
                reg.setdefault(mod, set()).update({sym, sym.replace("_", "-"), *([cmd] if cmd else [])})
        facts["registrar"] = {m: sorted(w | {m.rsplit(".", 1)[-1], m.rsplit(".", 1)[-1].replace("_", "-")}) for m, w in reg.items()}
    facts["words"] = sorted(words)[:4000]
    facts["substrate"] = is_test and bool(_SUBSTRATE_RE.search(text))
    facts["imports"] = sorted(imports)
    facts["modrefs"] = sorted(modrefs)
    facts["tokens"] = sorted(tokens)[:600]
    # a token seen only inside sentences: it selects a test file that names it, but a helper that merely mentions
    # a file reads nothing, so it must not count as changed and drag in every importer
    facts["mentions"] = sorted(prose - literal_tokens)
    facts["scans"] = "rglob(" in text or "**" in text
    return facts


# ── the index ────────────────────────────────────────────────────────────────


def _prefixes(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:k]) for k in range(1, len(parts) + 1)]


@dataclass
class Index:
    files: dict[str, dict]
    #: dotted/bare module name -> files answering to it
    modules: dict[str, list[str]] = field(default_factory=dict)
    #: name (and every parent package of it) -> files that import or name it
    importers: dict[str, set[str]] = field(default_factory=dict)
    #: path-ish token -> files naming it
    by_token: dict[str, set[str]] = field(default_factory=dict)
    by_last: dict[str, set[str]] = field(default_factory=dict)
    globs: list[tuple[str, str]] = field(default_factory=list)
    nx_spawners: set[str] = field(default_factory=set)
    substrate: set[str] = field(default_factory=set)
    #: registrar file -> {command module -> words that name its commands}
    registrars: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    entry_modules: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def link(self) -> None:
        pyproject = self.entry_modules
        for rel, f in self.files.items():
            for n in module_names(rel):
                self.modules.setdefault(n, []).append(rel)
            names = set(f["imports"]) | set(f["modrefs"])
            if f.get("substrate"):
                self.substrate.add(rel)
            if f["registrar"]:
                self.registrars[rel] = {m: set(w) for m, w in f["registrar"].items()}
            if f["spawns_nx"]:
                self.nx_spawners.add(rel)
                names.update(pyproject)
            for n in names:
                for p in _prefixes(n):
                    self.importers.setdefault(p, set()).add(rel)
            for tok in f["tokens"]:
                if "*" in tok or "?" in tok:
                    # a bare extension glob (``*.py``) names no file, but a test that walks the whole tree for
                    # that extension (rglob, ``**``) is a lint over every such file: any of them can break it
                    if _names_something(tok) or (f.get("scans") and _BARE_EXT_GLOB_RE.fullmatch(tok)):
                        self.globs.append((tok, rel))
                else:
                    self.by_token.setdefault(tok, set()).add(rel)
                    self.by_last.setdefault(tok.rsplit("/", 1)[-1], set()).add(tok)


def _entry_modules(repo: Path) -> list[str]:
    try:
        text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not m:
        return []
    return sorted(set(re.findall(r'^[\w-]+\s*=\s*"([\w.]+):\w+"', m.group(1), re.M)))


def _walk_py(repo: Path) -> list[str]:
    found: list[str] = []
    for root in SCAN_ROOTS:
        top = repo / root
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    found.append((Path(dirpath) / fn).relative_to(repo).as_posix())
    return sorted(found)


def build_index(repo: Path, use_cache: bool = True) -> Index:
    cache_path = repo / CACHE_REL
    cached: dict[str, list] = {}
    if use_cache and cache_path.is_file():
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            if blob.get("version") == CACHE_VERSION:
                cached = blob["files"]
        except (OSError, ValueError, KeyError):
            cached = {}
    files: dict[str, dict] = {}
    entries: dict[str, list] = {}
    parsed = reused = 0
    for rel in _walk_py(repo):
        try:
            st = (repo / rel).stat()
        except OSError:
            continue
        hit = cached.get(rel)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
            facts = hit[2]
            reused += 1
        else:
            try:
                text = (repo / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            facts = extract_facts(text, rel)
            parsed += 1
        files[rel] = facts
        entries[rel] = [st.st_size, st.st_mtime_ns, facts]
    if use_cache and parsed:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": entries}), encoding="utf-8")
            os.replace(tmp, cache_path)
        except OSError:
            pass
    idx = Index(files=files, entry_modules=_entry_modules(repo))
    idx.link()
    idx.stats = {"parsed": parsed, "cached": reused, "files": len(files)}
    return idx


# ── selection ────────────────────────────────────────────────────────────────


@dataclass
class Selection:
    files: dict[str, list[str]] = field(default_factory=dict)
    everything: str | None = None
    notes: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    #: lint/census tests that scripts/meta_tests.py runs whatever the diff (its manifest cannot place them).
    #: They carry marks pytest's default -m deselects, so a bare ``pytest <files>`` would run none of them;
    #: ``scripts/meta_tests.py run`` runs them, and ``--include-meta-always`` lists them here.
    meta_always: dict[str, str] = field(default_factory=dict)

    def add(self, rel: str, why: str) -> None:
        reasons = self.files.setdefault(rel, [])
        if why not in reasons and len(reasons) < 6:
            reasons.append(why)


def _display(rel: str) -> str:
    names = module_names(rel)
    return names[0] if names and rel.endswith(".py") and not rel.startswith("tests/test_") else rel


def _load_sibling(repo: Path, name: str):
    """scripts/<name>.py of the repo under analysis (a replay checks out an old tree), else ours."""
    for base in (repo / "scripts", SCRIPTS_DIR):
        path = base / f"{name}.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(f"_sat_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise FileNotFoundError(name)


def _command_words(idx: Index, registrar: str, cur_names: list[str]) -> set[str] | None:
    """The words naming the commands a registrar registers from the module cur answers to; None when the
    edge into the registrar is not a registration."""
    reg = idx.registrars.get(registrar)
    if not reg:
        return None
    words: set[str] = set()
    for m, w in reg.items():
        if m in cur_names:
            words |= w
    return words or None


def _specific(tok: str) -> bool:
    """True for a token naming one file (a path or a basename), not a directory or a glob."""
    return not tok.endswith("/") and "*" not in tok and "?" not in tok


def _file_references(idx: Index, p: str) -> list[tuple[str, str]]:
    """(file, matched token) for every indexed file that names the path p."""
    out: list[tuple[str, str]] = []
    base = p.rsplit("/", 1)[-1]
    for tok in idx.by_last.get(base, ()):
        if tok == p or p.endswith("/" + tok):
            out.extend((f, tok) for f in idx.by_token[tok])
    comps = p.split("/")[:-1]
    for i in range(len(comps)):
        for j in range(i + 1, len(comps) + 1):
            run = comps[i:j]
            key = "/".join(run)
            if len(run) == 1 and key not in SINGLE_DIRS:
                continue
            if len(run) == 1 and i != 0:
                continue
            for f in idx.by_token.get(key, ()):
                out.append((f, key + "/"))
    suffixes = ["/".join(comps[i:] + [base]) for i in range(len(comps) + 1)]
    for pat, f in idx.globs:
        if fnmatch.fnmatchcase(base, pat) or any(fnmatch.fnmatchcase(s, pat) for s in suffixes):
            out.append((f, pat))
    return out


def _norm_route(r: str) -> str | None:
    r = re.split(r"[{.]", r, maxsplit=1)[0].rstrip("/")
    return r.lstrip("/") if r.count("/") >= 1 and len(r) > 3 else None


def _service_routes(repo: Path, java: list[str]) -> dict[str, str]:
    routes: dict[str, str] = {}
    for rel in java:
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _ROUTE_RE.finditer(text):
            n = _norm_route(m.group(1))
            if n:
                routes.setdefault(n, rel)
    return routes


def _service(repo: Path, idx: Index, changed: list[str], sel: Selection, seeds: dict[str, str], service_substrate: bool) -> None:
    svc = [c for c in changed if c.startswith("service/")]
    if not svc:
        return
    main_java = [c for c in svc if c.startswith("service/src/main/java/") and c.endswith(".java")]
    classes: list[str] = []
    for c in svc:
        if not c.endswith(".java"):
            continue
        stem = c.rsplit("/", 1)[-1][:-5]
        if "/src/test/java/" in c:
            classes.append(stem)
            continue
        for suffix in ("Test", "IntegrationTest"):
            found = list((repo / "service/src/test/java").rglob(f"{stem}{suffix}.java")) if (repo / "service/src/test/java").is_dir() else []
            classes.extend(f.stem for f in found)
    classes = sorted(set(classes))
    if classes:
        sel.notes.append(
            "service/ changed: run the touched engine classes (never a heavy run unwrapped): "
            "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dsurefire.failIfNoSpecifiedTests=false "
            f"-Dtest={shlex.quote(','.join(classes))}"
        )
    else:
        sel.notes.append(
            "service/ changed with no engine test class matching a changed file by name: run the engine class for "
            "the area through scripts/mvnw-leased.sh --lab-run lab-m (no Python selection can stand in for it)"
        )
    main_changed = [c for c in svc if c.startswith("service/src/main/")]
    if main_changed:
        subs = sorted(f for f in idx.files if is_test_file(f) and f in idx.substrate)
        if service_substrate:
            for f in subs:
                sel.add(f, f"boots the engine substrate; {main_changed[0]} changed (--service-substrate)")
        else:
            sel.notes.append(
                f"service/ main changed: {len(subs)} Python test file(s) boot the engine substrate and run against the "
                "changed engine; not selected (--no-service-substrate; the routes below are the cheap derivation)"
            )
    routes = _service_routes(repo, main_java)
    if not routes:
        sel.notes.append("service/ change names no wire route in a changed Java source: no Python route readers selected")
        return
    for route, java in sorted(routes.items()):
        for tok, files in idx.by_token.items():
            if tok == route or tok.startswith(route + "/"):
                for f in files:
                    why = f"names wire route /{route}, served by {java}"
                    if is_test_file(f):
                        sel.add(f, why)
                    else:
                        seeds.setdefault(f, why)


def closure(idx: Index, seeds: dict[str, str], max_depth: int | None = None) -> dict[str, str | None]:
    """Import closure, breadth first over reverse edges: file -> the file it was reached from (None for a seed).

    A CLI registrar imports every command at top level; an edge from a command module into it is a
    registration, not a dependency of whoever imports the CLI, so a test that imports the CLI is reached only
    when it names that command (a word of the command, as in ["search", ...]); a tests/ helper is reached
    unconditionally; a src importer of the registrar is not reached through the registration."""
    pred: dict[str, str | None] = {s: None for s in seeds}
    depth: dict[str, int] = {s: 0 for s in seeds}
    queue = deque(seeds)
    while queue:
        cur = queue.popleft()
        if max_depth is not None and depth[cur] >= max_depth:
            continue
        names = module_names(cur)
        for n in names:
            for imp in idx.importers.get(n, ()):
                if imp in pred:
                    continue
                words = _command_words(idx, imp, names)
                if words is None:
                    pred[imp] = cur
                    depth[imp] = depth[cur] + 1
                    queue.append(imp)
                    continue
                for t in idx.importers.get(module_names(imp)[0], ()):
                    if t in pred or t == imp or not t.startswith("tests/"):
                        continue
                    if is_test_file(t) and not (set(idx.files[t]["words"]) & words):
                        continue
                    pred[t] = cur
                    depth[t] = depth[cur] + 1
                    queue.append(t)
    return pred


def select(
    repo: Path,
    changed: list[str],
    base: str | None = None,
    use_meta: bool = True,
    use_docs: bool = True,
    use_cache: bool = True,
    index: Index | None = None,
    include_always: bool = False,
    max_depth: int | None = None,
    service_substrate: bool = False,
) -> Selection:
    repo = Path(repo)
    changed = sorted({c.strip() for c in changed if c.strip()})
    sel = Selection(changed=changed)
    idx = index or build_index(repo, use_cache=use_cache)

    for c in changed:
        if c in RUN_EVERYTHING:
            sel.everything = sel.everything or f"{c} changed"

    seeds: dict[str, str] = {}
    for c in changed:
        if is_test_file(c) and (repo / c).is_file():
            sel.add(c, "the test file itself changed")
        if c.endswith(".py"):
            seeds[c] = "changed"
        elif c.startswith("src/nexus/"):
            # Package data (a toml, sql, lua or json beside the code) is read by the package's own modules,
            # often by scanning the directory, so the modules of that directory are changed.
            d = c.rsplit("/", 1)[0] + "/"
            for f in idx.files:
                if f.startswith(d) and "/" not in f[len(d):]:
                    seeds.setdefault(f, f"package data {c} changed")
        # non-Python files: whoever names them
        for f, tok in _file_references(idx, c):
            if f == c:
                continue
            why = f'names "{tok}" ({c} changed)'
            if is_test_file(f):
                sel.add(f, why)
            elif _specific(tok) and tok not in idx.files[f].get("mentions", ()) and ("/" in tok or c.startswith(f.rsplit("/", 1)[0] + "/")):
                # A module that reads the exact file (by path, or by basename from its own directory tree) is
                # itself changed. One that scans a directory, a glob or a bare "README.md" is not: that would
                # seed its whole import closure on every docs or plugin edit.
                seeds.setdefault(f, why)

    _service(repo, idx, changed, sel, seeds, service_substrate)

    pred = closure(idx, seeds, max_depth)
    # A deleted file still answers to its module name, so its importers are found by name above.

    def chain(f: str) -> str:
        hops = [f]
        while pred.get(hops[-1]) is not None:
            hops.append(pred[hops[-1]])  # type: ignore[arg-type]
        seed = hops[-1]
        path = " -> ".join(_display(h) if i else h for i, h in enumerate(hops))
        return f"{path} ({seeds[seed]})" if len(hops) > 1 else seeds[seed]

    def _root(p: dict[str, str | None], f: str) -> str:
        while p.get(f) is not None:
            f = p[f]  # type: ignore[assignment]
        return f

    conftests = [f for f in pred if f.endswith("/conftest.py") and f.startswith("tests/")]
    for f in pred:
        if is_test_file(f) and pred[f] is not None:
            sel.add(f, "imports " + chain(f))
    for cf in conftests:
        d = cf.rsplit("/", 1)[0]
        why = f"{cf} is reached by the change ({chain(cf)}); it applies to every test below {d}/"
        if d == "tests":
            # A tests/ helper the root conftest loads selects everything. A src module the root conftest
            # imports does not: that is every src change, and the backstop run covers fixtures that use it.
            if pred[cf] is not None and _root(pred, cf).startswith("tests/"):
                sel.everything = sel.everything or f"{_root(pred, cf)} changed and {cf} loads it ({chain(cf)})"
            continue
        for f in idx.files:
            if is_test_file(f) and f.startswith(d + "/"):
                sel.add(f, why)

    if use_docs and any(c.startswith(DOCS_TRIGGERS) or c in DOCS_FILES for c in changed):
        try:
            mod = _load_sibling(repo, "select_docs_reader_tests")
            for f in mod.select(repo.resolve()):
                sel.add(f, "docs reader: a docs/ path changed and this test reads docs/")
        except Exception as e:  # noqa: BLE001 - a broken selector must read as a note, never as "nothing"
            sel.notes.append(f"docs-reader selection failed ({type(e).__name__}: {e}); run scripts/select_docs_reader_tests.py")

    if use_meta:
        _meta(repo, changed, base, sel, include_always)

    # only tests that exist now can run
    for f in list(sel.files):
        if not (repo / f).is_file():
            del sel.files[f]
    return sel


def _meta(repo: Path, changed: list[str], base: str | None, sel: Selection, include_always: bool) -> None:
    if base is None:
        sel.notes.append("no base ref: meta-test selection skipped (scripts/meta_tests.py select --base <ref>)")
        return
    if not (repo / "tests/meta_surfaces.json.gz").is_file():
        sel.notes.append("tests/meta_surfaces.json.gz absent: meta-test selection skipped")
        return
    try:
        mt = _load_sibling(repo, "meta_tests")
        ms = mt.select(repo, changed, base)
    except BaseException as e:  # noqa: BLE001 - SystemExit included: meta_tests exits when conftest moves
        if isinstance(e, KeyboardInterrupt):
            raise
        sel.notes.append(f"meta_tests selection failed ({type(e).__name__}: {e}); run scripts/meta_tests.py select --base {base}")
        return
    for f, why in ms.selected.items():
        sel.add(f, f"meta-test: {why}")
    for f, why in ms.always.items():
        if include_always:
            sel.add(f, f"meta-test, runs whatever the diff: {why}")
        else:
            sel.meta_always[f] = why
    if ms.java:
        cmd = mt.java_command(ms)
        sel.notes.append(f"{len(ms.java)} Java meta-test(s) selected (engine source walkers): {cmd}")
    sel.notes.append(
        f"{len(ms.always)} lint/census test(s) run whatever the diff (the meta_tests manifest is stale for them) and "
        f"carry marks pytest's default -m deselects: run `scripts/meta_tests.py run --base {base}` beside the list"
    )


# ── cost estimate ────────────────────────────────────────────────────────────


def estimate(repo: Path, files: list[str], everything: bool) -> dict[str, float]:
    per_file: dict[str, list[float]] = {}
    try:
        data = json.loads((repo / DURATIONS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for node, secs in data.items():
        f = node.split("::", 1)[0]
        e = per_file.setdefault(f, [0.0, 0])
        e[0] += float(secs)
        e[1] += 1
    pick = per_file if everything else {f: per_file[f] for f in files if f in per_file}
    return {
        "files": float(len(files)),
        "tests": float(sum(v[1] for v in pick.values())),
        "serial_s": sum(v[0] for v in pick.values()),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def changed_from_range(repo: Path, rng: str) -> list[str]:
    out = _git(repo, "diff", "--no-renames", "--name-only", "-z", rng)
    return sorted(n for n in out.split("\0") if n)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("files", nargs="*", help="changed paths (repo-relative); otherwise --range, --files-from or --base")
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--base", help="diff base ref: base...HEAD plus the working tree and untracked files (default origin/develop)")
    ap.add_argument("--range", dest="rng", help="a git revision range, e.g. <before>..<after>")
    ap.add_argument("--files-from", help="a file of changed paths, one per line (- for stdin)")
    ap.add_argument("--explain", action="store_true", help="print why each file was picked")
    ap.add_argument("--max-depth", type=int, help="follow imports at most N hops from a changed module (a smoke tier; default: the full closure)")
    ap.add_argument("--service-substrate", action=argparse.BooleanOptionalAction, default=True,
                    help="on a service/src/main change, also select every Python test that boots the engine substrate "
                         "(default on: the 124-run replay's one diff-caused miss was missed only without it, "
                         "nexus_rdr/258-research-17; --no-service-substrate takes the cheap route derivation instead)")
    ap.add_argument("--no-meta", action="store_true")
    ap.add_argument("--include-meta-always", action="store_true",
                    help="also list the lint/census tests meta_tests.py runs whatever the diff (default: left to meta_tests.py run)")
    ap.add_argument("--no-docs", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="workers assumed by the time estimate")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()

    base = args.base
    if args.files:
        changed = args.files
    elif args.files_from:
        data = sys.stdin.read() if args.files_from == "-" else Path(args.files_from).read_text()
        changed = data.splitlines()
    elif args.rng:
        changed = changed_from_range(repo, args.rng)
        base = base or re.split(r"\.{2,3}", args.rng)[0] or None
    else:
        base = base or "origin/develop"
        try:
            mt = _load_sibling(repo, "meta_tests")
            changed = mt.changed_paths(repo, base)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            sys.stderr.write(f"select_affected_tests: cannot diff against {base}: {e}\n")
            return 2
    sel = select(repo, changed, base=base, use_meta=not args.no_meta, use_docs=not args.no_docs,
                 use_cache=not args.no_cache, include_always=args.include_meta_always,
                 max_depth=args.max_depth, service_substrate=args.service_substrate)

    files = sorted(sel.files)
    if args.explain:
        sys.stdout.write(f"changed: {len(sel.changed)} path(s)\n")
        if sel.everything:
            sys.stdout.write(f"EVERYTHING: {sel.everything}\n")
        for f in files:
            sys.stdout.write(f"{f}\n")
            for why in sel.files[f]:
                sys.stdout.write(f"    {why}\n")
    elif sel.everything:
        sys.stdout.write("tests\n")
    else:
        for f in files:
            sys.stdout.write(f + "\n")
    if sel.everything:
        sys.stderr.write(
            f"select_affected_tests: selecting everything (prints `tests`): {sel.everything}. "
            "Run the full suite via lab-remote/lab-run, not here.\n"
        )
    for n in sel.notes:
        sys.stderr.write(f"select_affected_tests: {n}\n")
    est = estimate(repo, files, bool(sel.everything))
    if est:
        sys.stderr.write(
            f"select_affected_tests: {len(files)} file(s), ~{int(est['tests'])} test(s); recorded time "
            f"{est['serial_s']:.0f}s serial, ~{est['serial_s'] / max(args.workers, 1):.0f}s at {args.workers} workers\n"
        )
    if not files and not sel.everything:
        sys.stderr.write("select_affected_tests: nothing selected; do not run a bare pytest\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
