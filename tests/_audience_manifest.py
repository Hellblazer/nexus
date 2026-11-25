# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read and check ``conexus/hooks/audience.yaml`` (nexus-sby49.3, plan section 5 item 1).

The manifest says, for every hook registration, every module that carries a rule or text, and every injected constant,
WHO it is for (``EVERY-USER``, ``NEXUS-REPO``, ``SAM-WIDE`` or ``REMOVE``) and what gates it. This module is the checker:
:func:`problems` returns every way the manifest and the code disagree, and the lint test and the behaviour leg both call it.

The checks, each a way a rule can reach the wrong audience unnoticed:

* every ``hooks.json`` registration has an entry, keyed on event plus argv (23 registrations, 22 distinct argv: auto-approve
  is registered for PreToolUse and PermissionRequest with one argv), and an entry names a registration that exists;
* every value is in its vocabulary, and an audience agrees with its gate (SAM-WIDE <-> ``maintainer`` or ``account_flag``,
  NEXUS-REPO <-> ``nexus_repo`` or the marker's own file, EVERY-USER <-> none or a gate on the user's data);
* a declared constant exists, and every module-level string constant of 40 characters or more in a hook module is declared
  as injected or as not injected, with a reason;
* a module that imports the audience helper declares exactly the predicates it imports, imports them inside a ``try`` whose
  fallback is the contract's (``True`` for a tightening predicate, ``False`` for ``account_flag``, an empty set for
  ``remote_test_hosts``, nothing for the notes), never calls one at import time, and calls each only inside a function
  that sits under one of the accepted wrapper shapes (``run_hook``, ``never_fail``, a verb the dispatcher wraps, an
  ``mcp`` hook tool, a ``main`` with a ``try``/``except``, or a local ``try``/``except`` around the call);
* a live entry whose gate names a predicate sits in a module that imports and calls it.

An entry that is ``live: false`` names a rule whose gate is not wired yet (a Phase 3 row); the behaviour leg's allowlist is
exactly the set of such rows.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

AUDIENCES = ("EVERY-USER", "NEXUS-REPO", "SAM-WIDE", "REMOVE")
GATES = ("none", "maintainer", "nexus_repo", "account_flag", "data")
PHASES = ("-", "1", "2", "3", "after-2", "4")
WRAPPERS = ("run_hook", "never_fail", "entry", "mcp", "main_try", "local_try", "none")

#: predicate -> what its import fallback must return. ``"void"`` is a note writer: it returns nothing.
PREDICATES: dict[str, str] = {
    "maintainer": "True",
    "nexus_repo": "True",
    "account_flag": "False",
    "remote_test_hosts": "frozenset()",
    "note_marker_only": "void",
    "note_hook_error": "void",
}
#: The gates an audience may carry. SAM-WIDE is the account flag or the marker (``maintainer``), or the flag alone for a
#: loosening; NEXUS-REPO is the marker predicate, or a data gate that is the marker's own file (the worktree preflight row
#: keys on the script's presence); EVERY-USER is nothing or a gate on the user's own data.
ALLOWED_GATES: dict[str, tuple[str, ...]] = {
    "SAM-WIDE": ("maintainer", "account_flag"),
    "NEXUS-REPO": ("nexus_repo", "data"),
    "EVERY-USER": ("none", "data"),
    "REMOVE": ("none", "data"),
}
#: A gate that names a predicate must be wired (``live``) in a module that calls it.
PREDICATE_GATES = ("maintainer", "nexus_repo", "account_flag")

AUDIENCE_MODULE_NAMES = ("_audience", "nexus.hooks._audience")
#: A module-level string constant this long is text somebody can read; shorter ones are names and fragments.
CENSUS_MIN_CHARS = 40
HOOK_DIRS = ("conexus/hooks/scripts", "src/nexus/hooks")
EXTRA_MODULES = ("src/nexus/session_start_guidance.py",)
MIN_REGISTRATIONS = 23
MAX_DECISION = 13

_ENTRY_FIELDS = ("audience", "gate", "evidence", "target_state", "phase")


# --- hooks.json ----------------------------------------------------------------------------------


def registration_key(event: str, hook: dict) -> tuple[str, list[str]]:
    """``(key, argv)``: the registration's identity. The argv is what follows ``python`` with the plugin root removed;
    an ``mcp_tool`` entry's is ``["mcp_tool", server, tool]``."""
    if hook.get("type") == "mcp_tool":
        argv = ["mcp_tool", str(hook["server"]), str(hook["tool"])]
    else:
        args = [str(a) for a in hook.get("args", [])]
        tail = args[args.index("python") + 1:] if "python" in args else args
        argv = [a.replace("${CLAUDE_PLUGIN_ROOT}/", "") for a in tail]
    return f"{event} {' '.join(argv)}", argv


def registrations(hooks_json: dict) -> list[dict]:
    out = []
    for event, groups in hooks_json["hooks"].items():
        for group in groups:
            for hook in group.get("hooks", []):
                key, argv = registration_key(event, hook)
                out.append({"key": key, "event": event, "matcher": group.get("matcher", ""), "argv": argv, "hook": hook})
    return out


def load_manifest(path: Path) -> dict:
    import yaml  # noqa: PLC0415 -- PyYAML is a project dependency; the leg runs under the project's interpreter

    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --- AST helpers -----------------------------------------------------------------------------------


def _assigned_constants(tree: ast.Module) -> dict[str, int | None]:
    """Module-level ``NAME = <literal>`` -> its length when it is a string literal, else ``None``."""
    out: dict[str, int | None] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                try:
                    literal = ast.literal_eval(value)
                except Exception:  # noqa: BLE001 -- not a literal: a computed value is not text a reader can pin
                    out.setdefault(target.id, None)
                    continue
                out[target.id] = len(literal) if isinstance(literal, str) else None
    return out


def census(tree: ast.Module) -> dict[str, int]:
    """Module-level string constants of :data:`CENSUS_MIN_CHARS` characters or more."""
    return {n: ln for n, ln in _assigned_constants(tree).items() if ln is not None and ln >= CENSUS_MIN_CHARS}


def _is_audience_module(name: str | None) -> bool:
    return name in AUDIENCE_MODULE_NAMES


def _catches_all(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = [e.id for e in (t.elts if isinstance(t, ast.Tuple) else [t]) if isinstance(e, ast.Name)]
    return "BaseException" in names or "Exception" in names


def _fallback_value(fn: ast.FunctionDef) -> str | None:
    """What a one-return fallback returns: ``"True"``, ``"False"``, ``"frozenset()"``, ``"void"`` or ``None`` (not that shape)."""
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    value = body[0].value
    if value is None:
        return "void"
    if isinstance(value, ast.Constant):
        if value.value is True:
            return "True"
        if value.value is False:
            return "False"
        if value.value is None:
            return "void"
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset"
            and not value.args and not value.keywords):
        return "frozenset()"
    return None


class _Audit(ast.NodeVisitor):
    """One pass over a module: the audience imports (with their guard and fallbacks) and every predicate call (with its place)."""

    def __init__(self) -> None:
        self.imports: list[dict[str, Any]] = []   # {alias, name, line, guarded, fallback_ok, fallback_why}
        self.bare_imports: list[int] = []
        self.calls: list[dict[str, Any]] = []     # {alias, line, in_function, local_try}
        self._try_stack: list[ast.Try] = []       # tries whose BODY we are inside
        self._fn_depth = 0
        self._class_depth = 0
        self._try_by_fn: list[int] = []           # len(self._try_stack) when each function was entered
        self._aliases: dict[str, str] = {}

    # imports ----------------------------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if _is_audience_module(a.name):
                self.bare_imports.append(node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "nexus.hooks" and any(a.name == "_audience" for a in node.names):
            self.bare_imports.append(node.lineno)
            return
        if not _is_audience_module(node.module):
            return
        guard = self._try_stack[-1] if self._try_stack else None
        for a in node.names:
            alias = a.asname or a.name
            self._aliases[alias] = a.name
            ok, why = self._fallback(guard, alias, a.name)
            self.imports.append({"alias": alias, "name": a.name, "line": node.lineno, "guarded": guard is not None,
                                 "fallback_ok": ok, "fallback_why": why})

    @staticmethod
    def _fallback(guard: ast.Try | None, alias: str, name: str) -> tuple[bool, str]:
        if guard is None:
            return False, "the import is not inside a try"
        if name not in PREDICATES:
            return False, f"{name!r} is not a predicate the checker knows"
        catching = [h for h in guard.handlers if _catches_all(h)]
        if not catching:
            return False, "the try has no handler for Exception or BaseException"
        want = PREDICATES[name]
        for h in catching:
            for stmt in h.body:
                if isinstance(stmt, ast.FunctionDef) and stmt.name == alias:
                    got = _fallback_value(stmt)
                    if got == want:
                        return True, ""
                    return False, f"the fallback for {alias} returns {got!r}, the contract needs {want!r}"
        return False, f"the handler defines no fallback named {alias}"

    # structure --------------------------------------------------------------------------------
    def visit_Try(self, node: ast.Try) -> None:
        self._try_stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self._try_stack.pop()
        for part in (node.handlers, node.orelse, node.finalbody):
            for stmt in part:
                self.visit(stmt)

    def _enter_fn(self, node: ast.AST) -> None:
        self._fn_depth += 1
        self._try_by_fn.append(len(self._try_stack))
        self.generic_visit(node)
        self._try_by_fn.pop()
        self._fn_depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _enter_fn

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name is not None and name in self._aliases:
            floor = self._try_by_fn[-1] if self._try_by_fn else 0
            handled = any(
                any(_catches_all(h) for h in t.handlers) for t in self._try_stack[floor:]
            )
            self.calls.append({"alias": name, "line": node.lineno, "in_function": self._fn_depth > 0,
                               "local_try": handled})
        self.generic_visit(node)


def audit_source(source: str) -> _Audit:
    audit = _Audit()
    audit.visit(ast.parse(source))
    return audit


def _has_call(tree: ast.Module, names: tuple[str, ...]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in names) or (isinstance(f, ast.Attribute) and f.attr in names):
                return True
    return False


def _main_has_try(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return any(isinstance(n, ast.Try) and any(_catches_all(h) for h in n.handlers) for n in ast.walk(node))
    return False


def module_problems(
    label: str, source: str, *, declared_predicates: list[str], wrapper: str,
    dotted: str | None = None, verb_modules: set[str] | None = None, mcp_modules: set[str] | None = None,
) -> list[str]:
    """Everything wrong with how one module uses the audience helper (T9 runs this on fixture modules)."""
    out: list[str] = []
    tree = ast.parse(source)
    audit = audit_source(source)
    imported = sorted({i["name"] for i in audit.imports})
    for line in audit.bare_imports:
        out.append(f"{label}:{line}: imports the audience helper as a module; use `from _audience import <predicate>` inside a try")
    for imp in audit.imports:
        if not imp["guarded"] or not imp["fallback_ok"]:
            out.append(f"{label}:{imp['line']}: import of {imp['name']}: {imp['fallback_why']}")
    if sorted(declared_predicates) != imported:
        out.append(f"{label}: declares predicates {sorted(declared_predicates)} but imports {imported}")
    wrapped = False
    if wrapper == "run_hook":
        wrapped = _has_call(tree, ("run_hook",))
    elif wrapper == "never_fail":
        wrapped = _has_call(tree, ("never_fail",))
    elif wrapper == "main_try":
        wrapped = _main_has_try(tree)
    elif wrapper == "entry":
        wrapped = bool(dotted and verb_modules is not None and dotted in verb_modules)
    elif wrapper == "mcp":
        wrapped = bool(dotted and mcp_modules is not None and dotted in mcp_modules)
    elif wrapper in ("local_try", "none"):
        wrapped = False
    else:
        out.append(f"{label}: unknown wrapper {wrapper!r}")
    if audit.calls and wrapper != "local_try" and not wrapped:
        out.append(f"{label}: calls a predicate but the wrapper shape {wrapper!r} is not present in the module")
    for call in audit.calls:
        where = f"{label}:{call['line']}: call to {call['alias']}"
        if not call["in_function"]:
            out.append(f"{where} runs at import time, outside any wrapped body")
        elif wrapper == "local_try" and not call["local_try"]:
            out.append(f"{where} is outside a try/except that catches Exception")
        elif wrapper in ("none",) and not call["local_try"]:
            out.append(f"{where} has no wrapper (declare run_hook, never_fail, entry, mcp, main_try or local_try)")
    return out


# --- the manifest -------------------------------------------------------------------------------


def _entry_problems(where: str, e: dict, *, allow_decision: bool = True) -> list[str]:
    out: list[str] = []
    for f in _ENTRY_FIELDS:
        if f not in e or e[f] in (None, ""):
            out.append(f"{where}: missing {f}")
    if out:
        return out
    if e["audience"] not in AUDIENCES:
        out.append(f"{where}: unknown audience {e['audience']!r}")
    if e["gate"] not in GATES:
        out.append(f"{where}: unknown gate {e['gate']!r}")
    if str(e["phase"]) not in PHASES:
        out.append(f"{where}: unknown phase {e['phase']!r}")
    live = e.get("live", True)
    if not isinstance(live, bool):
        out.append(f"{where}: live must be true or false")
    elif not live and str(e["phase"]) not in ("3", "after-2", "4"):
        out.append(f"{where}: a rule that is not live yet must name the phase that wires it (3, after-2 or 4)")
    elif live and str(e["phase"]) in ("after-2", "4"):
        out.append(f"{where}: a live rule cannot be phase {e['phase']}")
    aud, gate = e["audience"], e["gate"]
    if gate not in ALLOWED_GATES.get(aud, ()):
        out.append(f"{where}: audience {aud} cannot carry gate {gate!r} (allowed: {', '.join(ALLOWED_GATES.get(aud, ()))})")
    if "decision" in e:
        m = re.fullmatch(r"D(\d+)", str(e["decision"]))
        if not allow_decision or not m or not 1 <= int(m.group(1)) <= MAX_DECISION:
            out.append(f"{where}: decision {e['decision']!r} is not D1..D{MAX_DECISION}")
    return out


def hook_module_files(repo: Path) -> list[Path]:
    files: list[Path] = []
    for d in HOOK_DIRS:
        files += sorted((repo / d).rglob("*.py"))
    files += [repo / e for e in EXTRA_MODULES]
    return [f for f in files if "__pycache__" not in f.parts]


def _verb_modules(repo: Path) -> set[str]:
    import importlib  # noqa: PLC0415

    entry = importlib.import_module("nexus._hook_runtime.entry")
    return set(entry.VERB_TABLE.values())


def _mcp_modules(repo: Path) -> set[str]:
    tree = ast.parse((repo / "src/nexus/mcp/hooks.py").read_text(encoding="utf-8"))
    return {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("nexus.hooks.")}


def _dotted(repo: Path, path: str) -> str | None:
    p = Path(path)
    if p.parts[:2] == ("src", "nexus"):
        return ".".join(p.with_suffix("").parts[1:])
    return None


def problems(manifest: dict, hooks_json: dict, repo: Path, *, plugin_root: Path | None = None) -> list[str]:
    """Every disagreement between *manifest*, *hooks_json* and the code under *repo*. Empty means the manifest is true."""
    out: list[str] = []
    if manifest.get("version") != 1:
        out.append("manifest version must be 1")
    # -- registrations <-> hooks.json
    live_regs = registrations(hooks_json)
    keys = [r["key"] for r in live_regs]
    if len(live_regs) < MIN_REGISTRATIONS:
        out.append(f"hooks.json has {len(live_regs)} registrations, fewer than the floor {MIN_REGISTRATIONS}: a lint over a hollow plugin is not a pass")
    declared = manifest.get("registrations") or []
    seen: dict[str, dict] = {}
    for r in declared:
        k = r.get("key")
        if k in seen:
            out.append(f"registration {k!r} is declared twice")
        seen[str(k)] = r
    for r in live_regs:
        d = seen.get(r["key"])
        if d is None:
            out.append(f"hooks.json registration {r['key']!r} has no audience.yaml entry")
            continue
        if d.get("event") != r["event"] or d.get("matcher", "") != r["matcher"] or d.get("argv") != r["argv"]:
            out.append(f"registration {r['key']!r}: event, matcher or argv differ from hooks.json")
        out += _entry_problems(f"registration {r['key']!r}", d)
        for m in d.get("modules") or []:
            if not (repo / m).is_file():
                out.append(f"registration {r['key']!r}: module {m} does not exist")
        if not d.get("modules"):
            out.append(f"registration {r['key']!r}: names no implementing module")
    for k in seen:
        if k not in keys:
            out.append(f"audience.yaml names registration {k!r}, which hooks.json does not have")
    if len(set(keys)) != len(keys):
        out.append("hooks.json has two registrations with one key (event plus argv)")

    # -- modules table, constants, rules
    modules: dict[str, dict] = manifest.get("modules") or {}
    constants = manifest.get("constants") or []
    rules = manifest.get("rules") or []
    by_module: dict[str, dict[str, dict]] = {}
    for c in constants:
        cid = str(c.get("id", ""))
        if ":" not in cid:
            out.append(f"constant {cid!r}: id must be <module path>:<NAME>")
            continue
        mod, name = cid.rsplit(":", 1)
        out += _entry_problems(f"constant {cid}", c)
        if mod not in modules:
            out.append(f"constant {cid}: module {mod} is not in the modules table")
        by_module.setdefault(mod, {})[name] = c
    for r in rules:
        rid = str(r.get("id", ""))
        out += _entry_problems(f"rule {rid or '?'}", r)
        if r.get("module") not in modules:
            out.append(f"rule {rid}: module {r.get('module')} is not in the modules table")
    verb_modules = _verb_modules(repo)
    mcp_modules = _mcp_modules(repo)
    audited: dict[str, ast.Module] = {}
    for path, spec in modules.items():
        f = repo / path
        if not f.is_file():
            out.append(f"modules: {path} does not exist")
            continue
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        audited[path] = tree
        wrapper = str(spec.get("wrapper", "none"))
        if wrapper not in WRAPPERS:
            out.append(f"modules: {path}: unknown wrapper {wrapper!r}")
        out += module_problems(
            path, src, declared_predicates=list(spec.get("predicates") or []), wrapper=wrapper,
            dotted=_dotted(repo, path), verb_modules=verb_modules, mcp_modules=mcp_modules,
        )
        have = _assigned_constants(tree)
        injected = by_module.get(path, {})
        not_injected = spec.get("not_injected") or {}
        for name in injected:
            if name not in have:
                out.append(f"constant {path}:{name} does not exist")
        for name, reason in not_injected.items():
            if name not in have:
                out.append(f"modules: {path}: not_injected {name} does not exist")
            if not str(reason or "").strip():
                out.append(f"modules: {path}: not_injected {name} has no reason")
            if name in injected:
                out.append(f"modules: {path}: {name} is both injected and not_injected")
        for name in census(tree):
            if name not in injected and name not in not_injected:
                out.append(f"{path}:{name} is a string constant of {census(tree)[name]} characters that the manifest does not classify")
        imported = {i["name"] for i in audit_source(src).imports}
        for entry_id, e in [(f"constant {path}:{n}", c) for n, c in injected.items()] + [
            (f"rule {r.get('id')}", r) for r in rules if r.get("module") == path
        ]:
            want = e.get("gate") if e.get("gate") in PREDICATE_GATES else None
            if want is not None and e.get("live", True):
                calls = [c for c in audit_source(src).calls]
                alias_names = {i["alias"] for i in audit_source(src).imports if i["name"] == want}
                if want not in imported or not any(c["alias"] in alias_names for c in calls):
                    out.append(f"{entry_id}: audience {e['audience']} is live but {path} never calls {want}()")
    # -- every module that has text or a predicate is classified
    for f in hook_module_files(repo):
        rel = f.relative_to(repo).as_posix()
        if rel in modules:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        a = audit_source(f.read_text(encoding="utf-8"))
        implements = any(rel in (d.get("modules") or []) for d in declared)
        if census(tree) or a.imports or a.bare_imports or implements:
            why = "string constants" if census(tree) else ("an audience import" if (a.imports or a.bare_imports) else "a registration")
            out.append(f"{rel} has {why} and is not in the modules table")
    return out


def allowlist_rows(manifest: dict) -> list[str]:
    """The ids of every entry that is not live yet: the behaviour leg's allowlist must be exactly these."""
    rows: list[str] = []
    for kind, label in (("registrations", "key"), ("constants", "id"), ("rules", "id")):
        for e in manifest.get(kind) or []:
            if not e.get("live", True):
                rows.append(str(e[label]))
    return sorted(rows)


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)
