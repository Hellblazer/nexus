# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Decision 11 source rules of the audience lint (nexus-sby49.14, RDR-259).

``tests/_audience_manifest.py`` checks the manifest against the code. This module checks the code against the settings
model: nothing in the plugin or ``src/nexus/hooks/`` knows who the maintainer is, ``_audience.py`` reads one environment
variable, the telemetry senders read no endpoint, token, mint or proxy variable and start their children with the
environment ``telemetry_child_env`` builds, and ``hooks.json`` registers no ``PermissionRequest`` answer for the conexus
matcher. Every function takes source text or a parsed table and returns the problems it found, so a mutation of the real
source can be handed to it. The lint test (``tests/test_plugin_audience_lint.py``) runs each rule against the real tree
and against a mutation that must fail.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from tests import _audience_manifest as am

PLUGIN_SCRIPTS = "conexus/hooks/scripts"
SRC_HOOKS = "src/nexus/hooks"
AUDIENCE_COPIES = (f"{PLUGIN_SCRIPTS}/_audience.py", f"{SRC_HOOKS}/_audience.py")
ENDPOINT_RESOLVE = f"{PLUGIN_SCRIPTS}/_endpoint_resolve.py"
ROUTING_LIB = f"{PLUGIN_SCRIPTS}/routing/_lib.py"
DISPATCH_RECORD = f"{SRC_HOOKS}/agent_dispatch_record.py"
CONEXUS_MATCHER = "mcp__plugin_conexus_"

#: Names the old model gave a person or an account; no symbol in the plugin or ``src/nexus/hooks/`` may carry one.
RETIRED_SYMBOLS = frozenset({"maintainer", "account_flag", "maintainer_profile"})
#: The one place the retired file's name may be spelled: the constant ``show`` reads to say a leftover file has no effect.
RETIRED_FILE_NAME = "maintainer-profile"
RETIRED_FILE_CONSTANT = "RETIRED_USER_FILES"

#: The only environment variable ``_audience.py`` reads (the account home comes from the OS account database, never HOME).
AUDIENCE_ENV_ALLOWED = frozenset({"CLAUDE_PROJECT_DIR"})

#: What the telemetry senders must not read: where telemetry goes, the credential it carries, how it gets there.
TELEMETRY_FORBIDDEN_ENV = re.compile(
    r"^(NX_(SERVICE|MINT)_[A-Z]+|NX_CONFIG_DIR|XDG_CONFIG_HOME|(HTTPS?|ALL|NO)_PROXY|SSL_CERT_(FILE|DIR)|"
    r"REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|SSLKEYLOGFILE)$",
    re.IGNORECASE,
)
#: Named as allowed (Decision 9): the drop meter's log-path reads and the dispatch counter. ``NEXUS_CONFIG_DIR`` is the
#: account's own directory, which ``telemetry_child_env`` sets for the child.
TELEMETRY_ALLOWED_ENV = frozenset({"NX_DROPPED_WRITES_LOG_PATH", "NEXUS_CONFIG_DIR", "NX_DISPATCH_COUNTER"})
#: The drop meter is called from the senders and reads its own log path; the call graph does not descend into it.
DROP_METER_FUNCTIONS = frozenset({"_record_dropped_routing_event"})
#: Roots of the telemetry call path in ``_endpoint_resolve.py`` and in the routing library.
ENDPOINT_ROOTS = ("pinned_endpoint",)
POSTER_ROOTS = (
    "_send_routing_event", "_pinned_endpoint", "_spawn_poster", "_poster_main",
    "_post_routing_event_http", "_post_routing_event_once",
)
#: Functions of the routing library that resolved an endpoint from the environment before RDR-259 (nexus-ec37o's binding
#: wrapper and the resolver itself). They are gone; one that comes back is in lint scope and fails on its first env read.
RETIRED_RESOLVERS = ("_engine_endpoint", "_bound_token")


def poster_roots(source: str) -> tuple[str, ...]:
    """The poster's roots, plus any retired resolver the library still defines (so it cannot hide from the rule)."""
    present = {n.name for n in _tree(source).body if isinstance(n, ast.FunctionDef)}
    return POSTER_ROOTS + tuple(r for r in RETIRED_RESOLVERS if r in present)
CHILD_ENV_NAMES = ("telemetry_child_env", "_telemetry_child_env")

#: reader -> the setting whose failure value its import fallback must return, and how that value is spelled.
READER_SETTING: dict[str, str] = {
    "guard_mode": "subagent_git",
    "telemetry_enabled": "telemetry",
    "t1_nudge_enabled": "t1_nudge",
    "advice_blocked": "advice_block",
    "project_text": "preflight",
    "project_brief_questions": "brief_questions",
    "user_brief_questions": "brief_questions",
    "integration_branch": "integration_branch",
}


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _is_docstring(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    return isinstance(parent, ast.Expr) and isinstance(getattr(parent, "value", None), ast.Constant)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


# --- no maintainer, account_flag or maintainer-profile symbol ----------------------------------------


def retired_symbol_problems(source: str, where: str) -> list[str]:
    """Every identifier or string that names the retired model. The one exception is the file name inside
    ``RETIRED_USER_FILES`` (the constant ``nx settings show`` reads to report a leftover file)."""
    tree = _tree(source)
    parents = _parents(tree)
    out: list[str] = []

    def under_retired_constant(node: ast.AST) -> bool:
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.Assign, ast.AnnAssign)):
                targets = cur.targets if isinstance(cur, ast.Assign) else [cur.target]
                return any(isinstance(t, ast.Name) and t.id == RETIRED_FILE_CONSTANT for t in targets)
            cur = parents.get(cur)
        return False

    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.append(node.arg)
        elif isinstance(node, ast.alias):
            names.append(node.asname or node.name)
        for name in names:
            if name in RETIRED_SYMBOLS:
                out.append(f"{where}:{getattr(node, 'lineno', '?')}: the symbol {name!r} names the retired model")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and not _is_docstring(node, parents):
            text = node.value
            if text in RETIRED_SYMBOLS or (RETIRED_FILE_NAME in text and not under_retired_constant(node)):
                out.append(f"{where}:{node.lineno}: the string {text[:40]!r} names the retired model")
    return out


def retired_symbol_tree_problems(repo: Path) -> tuple[list[str], int]:
    """Run :func:`retired_symbol_problems` over every ``.py`` file of the plugin scripts and ``src/nexus/hooks/``, and scan
    the plugin's yaml and json for the retired tokens. Returns the problems and the number of files examined."""
    out: list[str] = []
    examined = 0
    for base in (PLUGIN_SCRIPTS, SRC_HOOKS):
        for path in sorted((repo / base).rglob("*.py")):
            examined += 1
            out += retired_symbol_problems(path.read_text(encoding="utf-8"), str(path.relative_to(repo)))
    for path in sorted((repo / "conexus" / "hooks").rglob("*")):
        if path.suffix in (".yaml", ".yml", ".json") and path.is_file():
            examined += 1
            text = path.read_text(encoding="utf-8")
            for token in ("account_flag", "maintainer_profile", RETIRED_FILE_NAME):
                if token in text:
                    out.append(f"{path.relative_to(repo)}: names the retired {token!r}")
    return out, examined


# --- _audience.py reads one environment variable ------------------------------------------------------


def _is_os_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr in ("environ", "environb")
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def audience_environ_problems(source: str, where: str = "_audience.py") -> list[str]:
    """An ``os.environ`` or ``os.getenv`` read in ``_audience.py`` other than ``CLAUDE_PROJECT_DIR`` by a literal key."""
    tree = _tree(source)
    out: list[str] = []
    allowed_nodes: set[int] = set()
    for node in ast.walk(tree):
        key: ast.AST | None = None
        base: ast.AST | None = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            if _is_os_environ(node.func.value) and node.func.attr in ("get", "pop", "setdefault"):
                base, key = node.func.value, node.args[0]
            elif node.func.attr == "getenv" and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                base, key = node.func, node.args[0]
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            base, key = node.value, node.slice
        if base is not None and isinstance(key, ast.Constant) and key.value in AUDIENCE_ENV_ALLOWED:
            allowed_nodes.add(id(base))
    for node in ast.walk(tree):
        if _is_os_environ(node) and id(node) not in allowed_nodes:
            out.append(f"{where}:{node.lineno}: reads os.environ other than {', '.join(sorted(AUDIENCE_ENV_ALLOWED))} by a literal key")
        elif isinstance(node, ast.Attribute) and node.attr == "getenv" and isinstance(node.value, ast.Name) \
                and node.value.id == "os" and id(node) not in allowed_nodes:
            out.append(f"{where}:{node.lineno}: reads os.getenv other than {', '.join(sorted(AUDIENCE_ENV_ALLOWED))} by a literal key")
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and any(a.name in ("environ", "getenv") for a in node.names):
            out.append(f"{where}:{node.lineno}: imports environ or getenv from os")
    return out


# --- the telemetry call path --------------------------------------------------------------------------


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def call_path(source: str, roots: tuple[str, ...], stop: frozenset[str] = frozenset()) -> dict[str, ast.FunctionDef]:
    """The top-level functions of *source* reachable from *roots* through calls and references by name, not descending
    into a function of *stop*. A root that does not exist raises: a rule over a path that is not there is not a pass."""
    funcs = _functions(_tree(source))
    missing = [r for r in roots if r not in funcs]
    if missing:
        raise LookupError(f"telemetry call path root(s) not found: {missing}")
    seen: dict[str, ast.FunctionDef] = {}
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in seen or name in stop or name not in funcs:
            continue
        seen[name] = funcs[name]
        for node in ast.walk(funcs[name]):
            if isinstance(node, ast.Name) and node.id in funcs:
                todo.append(node.id)
    return seen


def telemetry_read_problems(source: str, roots: tuple[str, ...], where: str,
                            stop: frozenset[str] = DROP_METER_FUNCTIONS) -> list[str]:
    """A read of an endpoint, token, mint or proxy variable on the call path from *roots*: any string constant that names one,
    and any environment read by a computed name."""
    out: list[str] = []
    for name, fn in call_path(source, roots, stop).items():
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and not isinstance(node.value, bool):
                if TELEMETRY_FORBIDDEN_ENV.match(node.value) and node.value not in TELEMETRY_ALLOWED_ENV:
                    out.append(f"{where}:{node.lineno}: {name} names the environment variable {node.value!r} on the telemetry path")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                is_get = (_is_os_environ(node.func.value) and node.func.attr in ("get", "pop")) or (
                    node.func.attr == "getenv" and isinstance(node.func.value, ast.Name) and node.func.value.id == "os")
                if is_get and not isinstance(node.args[0], ast.Constant):
                    out.append(f"{where}:{node.lineno}: {name} reads an environment variable by a computed name on the telemetry path")
    return out


def _child_env_names(fn: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            if isinstance(f, ast.Name) and f.id in CHILD_ENV_NAMES:
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def telemetry_child_problems(source: str, where: str) -> tuple[list[str], int]:
    """Every ``subprocess.Popen`` of *source* must pass ``env=`` a name the same function took from ``telemetry_child_env``,
    directly or through a ``**options`` dict literal with an ``"env"`` key. Returns the problems and the Popen calls seen."""
    out: list[str] = []
    seen = 0
    for fn in (n for n in ast.walk(_tree(source)) if isinstance(n, ast.FunctionDef)):
        good = _child_env_names(fn)
        dict_envs: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and k.value == "env" and isinstance(v, ast.Name) and v.id in good:
                        dict_envs |= {t.id for t in targets if isinstance(t, ast.Name)}
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"):
                continue
            seen += 1
            direct = any(k.arg == "env" and isinstance(k.value, ast.Name) and k.value.id in good for k in node.keywords)
            spread = any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id in dict_envs for k in node.keywords)
            if not (direct or spread):
                out.append(f"{where}:{node.lineno}: {fn.name} starts a telemetry child with an environment telemetry_child_env did not build")
    return out, seen


# --- hooks.json ---------------------------------------------------------------------------------------


def permission_request_problems(hooks_json: dict, manifest: dict | None = None) -> list[str]:
    """A ``PermissionRequest`` registration for the conexus matcher, in ``hooks.json`` or in the manifest."""
    out: list[str] = []
    for group in (hooks_json.get("hooks") or {}).get("PermissionRequest", []):
        if str(group.get("matcher", "")).startswith(CONEXUS_MATCHER):
            out.append(f"hooks.json registers PermissionRequest for {group.get('matcher')!r}: a PreToolUse answer does not override a user's own ask rule, a PermissionRequest one does")
    for r in (manifest or {}).get("registrations") or []:
        if r.get("event") == "PermissionRequest" and str(r.get("matcher", "")).startswith(CONEXUS_MATCHER):
            out.append(f"audience.yaml declares a PermissionRequest registration for {r.get('matcher')!r}")
    return out


# --- SETTINGS against the manifest and the readers ----------------------------------------------------


def settings_keys(audience_source: str) -> dict[str, Any]:
    """``{key: failure value}`` of the ``Setting(...)`` rows in ``_audience.py``'s table, read from the source."""
    tree = _tree(audience_source)
    names: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant):
            names[node.targets[0].id] = node.value.value
    keys: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Setting" and len(node.args) >= 7:
            key, failure = node.args[0], node.args[6]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys[key.value] = names[failure.id] if isinstance(failure, ast.Name) else ast.literal_eval(failure)
    return keys


def _spell(value: Any, reader: str) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, str):
        return repr(value)
    if value == []:
        return "frozenset()" if reader == "advice_blocked" else "[]"
    return repr(value)


def settings_problems(audience_source: str, manifest: dict) -> list[str]:
    """The checker's key table, the manifest and the readers against ``SETTINGS``: every ``SETTING:<key>`` names a key in the
    table; every key of the table has a row or a reader; a reader's import fallback returns the key's failure value."""
    out: list[str] = []
    table = settings_keys(audience_source)
    if len(table) < 10:
        return [f"the SETTINGS table has {len(table)} rows in _audience.py: a lint over a hollow table is not a pass"]
    real = set(table) - {"version"}
    if set(am.SETTING_READERS) != real:
        out.append(f"the checker's setting table {sorted(am.SETTING_READERS)} differs from SETTINGS {sorted(real)}")
    entries = [*(manifest.get("registrations") or []), *(manifest.get("constants") or []), *(manifest.get("rules") or [])]
    used_by_rows = {am.audience_setting(str(e.get("audience"))) for e in entries} - {None}
    for key in sorted(used_by_rows - real):
        out.append(f"audience SETTING:{key} names a key that is not in SETTINGS")
    tree = _tree(audience_source)
    read_in_functions: set[str] = set()
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")):
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in real:
                read_in_functions.add(node.value)
    for reader, key in READER_SETTING.items():
        if any(isinstance(n, ast.FunctionDef) and n.name == reader for n in tree.body):
            read_in_functions.add(key)
    for key in sorted(real - used_by_rows - read_in_functions):
        out.append(f"SETTINGS key {key!r} has no manifest row and no reader")
    for reader, key in READER_SETTING.items():
        want = _spell(table[key], reader)
        if am.PREDICATES.get(reader) != want:
            out.append(f"the reader {reader}'s fallback must return {am.PREDICATES.get(reader)!r}, SETTINGS[{key!r}].failure is {want!r}")
    return out
