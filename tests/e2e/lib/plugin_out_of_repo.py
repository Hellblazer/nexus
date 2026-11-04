# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host side of tests/e2e/plugin-out-of-repo-gate.sh (nexus-z7ddd).

The conexus plugin is checked everywhere from inside the nexus checkout, where
`../resources/agent-shared/...`, `conexus/registry.yaml` and `scripts/...` all
happen to open. A user runs it from a project that has none of those. This
module holds the judgements the gate makes, with no model call in the static
one:

  static   Every file reference in agents/, commands/, skills/ and resources/
           markdown, after substituting ${CLAUDE_PLUGIN_ROOT}, classified:

             RESOLVES       an absolute path (after substitution) inside the
                            plugin that exists
             RELATIVE-NEAR  a relative path that opens from the containing
                            file's own directory, in a skill or a resource
                            (the consumer learns that directory: a skill is
                            loaded with "Base directory for this skill", a
                            resource is Read by absolute path). Not a failure.
                            Tried BEFORE REPO-ONLY: a skill's own scripts/run.py
                            or docs/n.md next to its SKILL.md opens.
             REPO-ONLY      a path that exists only in the nexus repository
                            (see "Repo-only roots" below)
             RELATIVE-DEAD  a relative path with no directory the consumer
                            knows: any relative path in an agent or command
                            (their text carries no location), or one that
                            opens from nowhere
             MISSING        an absolute path that does not exist
             OUTSIDE        an absolute path outside the plugin
             URL            a URL or catalog scheme; not a file
             PLACEHOLDER    a glob, a $VAR, a <name>, a ~ path, NNN: not a file
             PROJECT        a path in the user's own project (docs/rdr/...)
             UNSUBSTITUTED  a plugin-root variable the model would see literally

           What Claude Code substitutes, MEASURED by plugin_canary.py on every
           gate run (it fails when a measurement contradicts these constants,
           READ_FILES_SUBSTITUTED and UNBRACED_SUBSTITUTED):

             ${CLAUDE_PLUGIN_ROOT}  in an agent's body, a skill's SKILL.md and a
                                    command's body: substituted (RESOLVES).
             ${CLAUDE_PLUGIN_ROOT}  in a file the model Reads (a resource, a
                                    skill's reference.md or any other file under
                                    skills/): NOT substituted. The Read result
                                    holds the literal text. UNSUBSTITUTED.
             $CLAUDE_PLUGIN_ROOT    without braces, anywhere: NOT substituted.
                                    UNSUBSTITUTED.
             in hook-injected markdown (hooks/**/*.md, which a hook prints):
                                    never substituted. UNSUBSTITUTED.

           A frontmatter line is treated like the body of its file (unmeasured:
           the model never sees it).

           A directory counts as a file: a reference to a directory that exists
           is RESOLVES, or RELATIVE-NEAR from a skill or resource.

           REPO-ONLY, RELATIVE-DEAD, MISSING, OUTSIDE and UNSUBSTITUTED fail the
           gate, unless the reference has an entry in the allowlist.

           Surfaces: agents, commands, skills and resources (the census), and
           hooks: every .md file under hooks/ except README.md, which are the
           files a hook script reads and prints into the model's context (the sn
           *-section.md files). A hook-injected file has no location, so it is
           judged like an agent: a relative path is RELATIVE-DEAD.

           What the extraction sees. A markdown link target; a backticked token
           with a directory part and a file extension; every whitespace-separated
           token of a backtick span that holds spaces; every such token of a
           fenced code block (``` or ~~~); and, in any line, a
           ${CLAUDE_PLUGIN_ROOT}/... or $CLAUDE_PLUGIN_ROOT/... path. What it
           does not see: a bare path in running prose, outside backticks and
           fences (it cannot tell `a/b.md` from "and/or" without a model), and a
           path assembled from parts. The live sweep reads the model's own list
           of references and covers part of that gap.

           Repo-only roots. A path is REPO-ONLY when its first segment is a
           directory of the nexus repository that no installed plugin has
           (conexus, mcpb, sn, tool-spill, prose-edit, .claude-plugin, .github,
           or src/nexus/...). A GENERIC project directory (src, tests, scripts,
           docs, web, service, .claude) is REPO-ONLY only when the path exists in
           the nexus checkout this gate runs from; a user's own scripts/deploy.sh
           or docs/design.md, which the checkout does not hold, is PROJECT. The
           cost: a typo'd nexus path under a generic directory reads PROJECT, not
           REPO-ONLY; the plugin's own tests and review catch those, not this gate.

           NOT-APPLICABLE. A plugin with no agents, commands, skills or resource
           markdown (sn, conexus-tool-spill) has no census to take. It reports
           NOT-APPLICABLE for that census instead of failing as "a census of
           nothing"; its hook markdown still gets the static pass. A plugin that
           HAS agents, commands, skills or resources and yields no reference in
           them still fails (the nexus-moht0 non-vacuity rule). Which plugin may
           be NOT-APPLICABLE is pinned by the gate script (conexus and
           conexus-prose-edit must be APPLICABLE, sn HOOKS-ONLY): a hollow plugin
           is not a pass.

           Not scanned: the text that src/nexus/session_start_guidance.py and
           src/nexus/hooks/subagent_start.py GENERATE and inject. It is Python
           source outside every plugin tree, so this gate has no file to read;
           the repo's own tests of those modules own it.

  allowlist  A checked-in file (tests/e2e/plugin-out-of-repo-allowlist.txt),
           one entry per line:  <plugin> | <plugin-relative file> | <target as
           written> | <class> | <reason>.  No line number: the key is the file,
           the target and the class. The plugin must be one of the four plugins
           (or the plugin under test). The class is one of the failing classes. The
           reason must say why the reference is acceptable: at least three words.
           The file is refused when a line is malformed, a name or a class is
           unknown, a reason is too thin, or a key is repeated; an entry for this
           plugin is STALE, and fails, when its file or target is no longer in
           the plugin, or the reference no longer fails with that class. The
           shipped file is empty: an entry is a decision, not a habit.

  live     The sweep (opt-in, advisory; plugin-gate --sweep). One `claude -p`
           per agent and per skill: the model quotes each file reference in the
           prompt it was given and Reads it. Judged on the Read events in the
           stream first and on the model's REF lines only as a fallback, and only
           on DEFINITE evidence: a Read error on a path the static pass says
           exists, a literal variable in a Read, a relative target quoted in an
           agent, a path opened from a nexus checkout outside the plugin. A run
           that shows nothing either way (the model quoted nothing, read the
           wrong path, or never answered) is UNPROBED: counted, named, retried at
           most twice, never red. A coverage floor demands at least one OPENED
           reference per mechanism (agent variable, skill variable, skill
           relative) that the plugin has; short of it the sweep is UNVERIFIED.

Usage:
  plugin_out_of_repo.py static  --plugin DIR [--json] [--allowlist FILE]
  plugin_out_of_repo.py targets --plugin DIR
  plugin_out_of_repo.py applicability --plugin DIR
  plugin_out_of_repo.py meta --plugin DIR              name<TAB>version
  plugin_out_of_repo.py live    --plugin DIR --events-dir DIR [--json] [--allowlist FILE]
                                [--partial] [--retry-list]
  (all: --repo-root DIR names the nexus checkout; --known-plugin NAME adds a plugin name)

Exit: 0 clean, 1 a failure, 2 UNVERIFIED (the live pass produced no result, or
missed its coverage floor), 64 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

#: What Claude Code does with the plugin-root variable. plugin_canary.py MEASURES both on every
#: gate run and fails when a measurement contradicts one of them, so a Claude Code release that
#: changes substitution turns the gate red instead of leaving the classifier wrong.
READ_FILES_SUBSTITUTED = False    # a file the model Reads (a resource, a skill's reference file)
UNBRACED_SUBSTITUTED = False      # $CLAUDE_PLUGIN_ROOT without braces, in a body that is substituted

CENSUS_SURFACES = ("agents", "commands", "skills", "resources")
SURFACES = CENSUS_SURFACES + ("hooks",)
LIVE_SURFACES = ("agents", "skills")

FAILING = frozenset({"REPO-ONLY", "RELATIVE-DEAD", "MISSING", "OUTSIDE", "UNSUBSTITUTED"})
CLASSES = (
    "RESOLVES", "RELATIVE-NEAR", "REPO-ONLY", "RELATIVE-DEAD", "MISSING",
    "OUTSIDE", "UNSUBSTITUTED", "URL", "PLACEHOLDER", "PROJECT",
)
NON_FILE = frozenset({"URL", "PLACEHOLDER", "PROJECT"})
KNOWN_PLUGINS = frozenset({"conexus", "sn", "conexus-tool-spill", "conexus-prose-edit"})

#: First path segments that exist in the nexus repository and in no installed plugin.
STRONG_REPO_ROOTS = frozenset({
    "conexus", "mcpb", "sn", "tool-spill", "prose-edit", ".claude-plugin", ".github",
})
#: Directories a user's own project is likely to have too: REPO-ONLY only when the path
#: exists in the nexus checkout. `docs/rdr/` is carved out as PROJECT: the RDR skills name
#: the user's own directory with it.
GENERIC_REPO_ROOTS = frozenset({"src", "tests", "scripts", "docs", "web", "service", ".claude"})
PROJECT_ROOTS = frozenset({".serena", ".beads", ".git"})
EXTS = ("md", "yaml", "yml", "json", "sh", "py", "txt", "toml", "xml", "java", "cfg")

NEXUS_CHECKOUT: str | None = None   # set by main() (--repo-root, else detected from this file)

_BRACED = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}")
_UNBRACED = re.compile(r"\$CLAUDE_PLUGIN_ROOT\b")
_ROOT_VAR = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b")
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_ROOT_PATH = re.compile(r"(?:\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b)/[A-Za-z0-9_./\-]+")
_TICK = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"^\s*(```|~~~)")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://|^mailto:", re.I)
_PATHISH = re.compile(r"^[~.$A-Za-z0-9_{}<>*\-/.:#@\[\]]+$")
_BASEDIR = re.compile(r"Base directory for this skill: ([^\\\"\n]+)")
_LINESUF = re.compile(r":\d+(?:-\d+)?$")
_TOKEN_EDGE = " \t\"'()[],;"


@dataclass(frozen=True)
class Ref:
    surface: str
    rel_file: str      # path relative to the plugin dir
    line: int
    raw: str           # as written (suffixes stripped)
    cls: str = ""
    isdir: bool = False   # the target is a directory (a live Read cannot open one)
    allowed: str = ""     # the allowlist reason, when an entry covers this reference


def _strip(t: str) -> str:
    t = t.strip().strip("`").rstrip(".,;")
    t = t.split("#", 1)[0]
    return _LINESUF.sub("", t)


def plugin_files(plugin: str):
    for surface in SURFACES:
        root = os.path.join(plugin, surface)
        for dirpath, dirs, files in os.walk(root):
            dirs.sort()
            for f in sorted(files):
                if not f.endswith(".md"):
                    continue
                if surface == "hooks" and f.lower() == "readme.md":
                    continue   # documentation, not text a hook injects
                yield surface, os.path.join(dirpath, f)


def _file_token(tok: str) -> str | None:
    """A whitespace-separated token of code that names a file: a directory part and an extension."""
    tok = tok.strip(_TOKEN_EDGE)
    if "=" in tok:
        tok = tok.rsplit("=", 1)[1].strip(_TOKEN_EDGE)
    if "/" not in tok or not _PATHISH.match(tok):
        return None
    base = _strip(tok)
    leaf = base.rsplit("/", 1)[-1]
    if "." not in leaf or base.rsplit(".", 1)[-1] not in EXTS:
        return None
    return tok


def extract(text: str):
    """Yield (line number, target as written) for every file-ish reference. Scope: the module docstring."""
    fenced = False
    for no, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        seen: set[str] = set()
        found: list[str] = []
        if not fenced:
            for m in _LINK.finditer(line):
                found.append(m.group(1))
        for m in _ROOT_PATH.finditer(line):
            found.append(m.group(0))
        if fenced:
            for tok in line.split():
                t = _file_token(tok)
                if t:
                    found.append(t)
        else:
            for m in _TICK.finditer(line):
                body = m.group(1).strip()
                if " " in body:
                    for tok in body.split():
                        t = _file_token(tok)
                        if t:
                            found.append(t)
                    continue
                if "/" not in body or not _PATHISH.match(body):
                    continue
                base = _strip(body)
                if base.rsplit(".", 1)[-1] not in EXTS or "." not in base.rsplit("/", 1)[-1]:
                    continue
                found.append(body)
        for raw in found:
            t = _strip(raw)
            if not t or t.startswith("#") or t in seen:
                continue
            seen.add(t)
            yield no, t


def canon(p: str) -> str:
    """A path as a comparison key. Claude Code reports the path it was given (on macOS /var/folders/...),
    the gate resolves symlinks (/private/var/folders/...): both sides go through here."""
    return os.path.realpath(p) if p.startswith("/") else os.path.normpath(p) if p else p


def _under(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _exists(path: str) -> bool:
    """A file or a directory: a reference may name either."""
    return os.path.isfile(path) or os.path.isdir(path)


def _substituted_text(surface: str, file_abs: str) -> bool:
    """True when Claude Code substitutes ${CLAUDE_PLUGIN_ROOT} in this file's text before the model sees it."""
    if surface in ("agents", "commands"):
        return True
    if surface == "skills":
        return os.path.basename(file_abs) == "SKILL.md"
    return READ_FILES_SUBSTITUTED and surface == "resources"


def _target_path(target: str, surface: str, file_abs: str, plugin: str) -> str | None:
    """The absolute path a reference opens, or None when it opens nothing."""
    sub = _BRACED.sub(plugin, target)
    if sub.startswith("/"):
        return sub if _exists(sub) else None
    if surface in ("skills", "resources"):
        near = os.path.normpath(os.path.join(os.path.dirname(file_abs), sub))
        if _exists(near) and _under(near, plugin):
            return near
    return None


def _repo_only(sub: str) -> str | None:
    """REPO-ONLY, PROJECT, or None (a first segment that is neither)."""
    first = sub.split("/", 1)[0]
    if first in STRONG_REPO_ROOTS or sub.startswith("src/nexus/"):
        return "REPO-ONLY"
    if first in GENERIC_REPO_ROOTS:
        if NEXUS_CHECKOUT and _exists(os.path.join(NEXUS_CHECKOUT, sub)):
            return "REPO-ONLY"
        return "PROJECT"
    return None


def classify(target: str, surface: str, file_abs: str, plugin: str) -> str:
    if _URL.match(target):
        return "URL"
    if _ROOT_VAR.search(target):
        literal = (surface == "hooks" or not _substituted_text(surface, file_abs)
                   or (_UNBRACED.search(target) and not UNBRACED_SUBSTITUTED))
        if literal:
            return "UNSUBSTITUTED"
    sub = _BRACED.sub(plugin, target)
    if (any(c in sub for c in "*<>{}$") or sub.startswith("~") or "NNN" in sub
            or "..." in sub or "YYYY" in sub):
        return "PLACEHOLDER"
    if sub.startswith("/"):
        if not _exists(sub):
            return "MISSING"
        return "RESOLVES" if _under(sub, plugin) else "OUTSIDE"
    first = sub.split("/", 1)[0]
    if sub.startswith("docs/rdr/") or first in PROJECT_ROOTS:
        return "PROJECT"
    if surface in ("skills", "resources"):
        near = os.path.normpath(os.path.join(os.path.dirname(file_abs), sub))
        if _exists(near) and _under(near, plugin):
            return "RELATIVE-NEAR"
    return _repo_only(sub) or "RELATIVE-DEAD"


MIN_REASON_WORDS = 3   # a sentence, not a placeholder


def load_allowlist(path: str | None, known: frozenset[str] = KNOWN_PLUGINS
                   ) -> tuple[dict[tuple[str, str, str, str], str], list[str]]:
    """-> ({(plugin, file, target, class): reason}, errors). A bad line is an error, never skipped."""
    allow: dict[tuple[str, str, str, str], str] = {}
    errors: list[str] = []
    if not path:
        return allow, errors
    try:
        fh = open(path, encoding="utf-8")
    except OSError as e:
        return allow, [f"{path}: cannot be read ({e.strerror})"]
    with fh:
        for no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|", 4)]
            where = f"{os.path.basename(path)}:{no}"
            if len(parts) != 5:
                errors.append(f"{where}: want '<plugin> | <file> | <target> | <class> | <reason>', got {len(parts)} field(s)")
                continue
            plugin, rel, target, cls, reason = parts
            if not (plugin and rel and target and cls):
                errors.append(f"{where}: plugin, file, target and class must all be given")
                continue
            if plugin not in known:
                errors.append(f"{where}: unknown plugin {plugin!r} (one of: {', '.join(sorted(known))})")
                continue
            if cls not in FAILING:
                errors.append(f"{where}: class {cls!r} is not a failing class (one of: {', '.join(sorted(FAILING))})")
                continue
            if len(reason.split()) < MIN_REASON_WORDS:
                errors.append(f"{where}: the reason is {'empty' if not reason else 'too short'} "
                              f"(write why {target} is acceptable, at least {MIN_REASON_WORDS} words)")
                continue
            key = (plugin, rel, target, cls)
            if key in allow:
                errors.append(f"{where}: repeats an earlier entry for {rel} {target} {cls}")
                continue
            allow[key] = reason
    return allow, errors


def static_census(plugin: str, allow: dict | None = None) -> list[Ref]:
    allow = allow or {}
    name = plugin_name(plugin)
    refs: list[Ref] = []
    for surface, path in plugin_files(plugin):
        rel = os.path.relpath(path, plugin)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for no, target in extract(text):
            cls = classify(target, surface, path, plugin)
            opened = _target_path(target, surface, path, plugin)
            reason = allow.get((name, rel, target, cls), "") if cls in FAILING else ""
            refs.append(Ref(surface, rel, no, target, cls,
                            bool(opened and os.path.isdir(opened)), reason))
    return refs


def stale_entries(plugin: str, refs: list[Ref], allow: dict) -> list[str]:
    """Allowlist entries for this plugin that no longer cover a failing reference of their class."""
    name = plugin_name(plugin)
    files = {os.path.relpath(p, plugin) for _s, p in plugin_files(plugin)}
    out = []
    for (pl, rel, target, cls), _reason in sorted(allow.items()):
        if pl != name:
            continue
        here = [r for r in refs if r.rel_file == rel and r.raw == target]
        if rel not in files:
            out.append(f"STALE {rel}  {target}  [the file is no longer in the plugin]")
        elif not here:
            out.append(f"STALE {rel}  {target}  [the target is no longer in the file]")
        elif not any(r.cls == cls for r in here):
            out.append(f"STALE {rel}  {target}  [the reference is {here[0].cls} now, not {cls}; "
                       f"{'fix the entry' if here[0].cls in FAILING else 'drop the entry'}]")
    return out


def applicability(plugin: str) -> tuple[str, int, int]:
    """-> (kind, census files, hook files). kind: APPLICABLE | HOOKS-ONLY | NO-MARKDOWN."""
    census = sum(1 for s, _ in plugin_files(plugin) if s in CENSUS_SURFACES)
    hooks = sum(1 for s, _ in plugin_files(plugin) if s == "hooks")
    kind = "APPLICABLE" if census else ("HOOKS-ONLY" if hooks else "NO-MARKDOWN")
    return kind, census, hooks


def _known_for(plugin: str, extra: frozenset[str]) -> frozenset[str]:
    return KNOWN_PLUGINS | extra | {plugin_name(plugin)}


def _static_report(plugin: str, as_json: bool, allow_path: str | None = None,
                   extra_known: frozenset[str] = frozenset()) -> int:
    allow, errors = load_allowlist(allow_path, _known_for(plugin, extra_known))
    if errors:
        for e in errors:
            print(f"  allowlist: {e}")
        print("static pass FAILED: the allowlist is refused; fix it before the census means anything")
        return 1
    refs = static_census(plugin, allow)
    kind, census_files, hook_files = applicability(plugin)
    files = Counter(s for s, _ in plugin_files(plugin))
    table: dict[str, Counter] = defaultdict(Counter)
    for r in refs:
        table[r.surface][r.cls] += 1
    failing = [r for r in refs if r.cls in FAILING and not r.allowed]
    allowed = [r for r in refs if r.allowed]
    stale = stale_entries(plugin, refs, allow)
    census_refs = sum(1 for r in refs if r.surface in CENSUS_SURFACES)
    if as_json:
        print(json.dumps({
            "plugin": plugin,
            "applicability": kind,
            "files": dict(files),
            "counts": {s: dict(c) for s, c in table.items()},
            "total": len(refs),
            "failing": [r.__dict__ for r in failing],
            "allowed": [r.__dict__ for r in allowed],
            "stale": stale,
        }, indent=1))
        return 1 if failing or stale or (kind == "APPLICABLE" and not census_refs) else 0
    print(f"static pass: {plugin}")
    print(f"  {sum(files.values())} markdown files "
          + ", ".join(f"{s}={files[s]}" for s in SURFACES) + f"; {len(refs)} references")
    head = f"  {'surface':<10}" + "".join(f"{c:>14}" for c in CLASSES)
    print(head)
    for s in SURFACES:
        print(f"  {s:<10}" + "".join(f"{table[s][c]:>14}" for c in CLASSES))
    tot = Counter(r.cls for r in refs)
    print(f"  {'TOTAL':<10}" + "".join(f"{tot[c]:>14}" for c in CLASSES))
    bad = bool(failing or stale)
    if kind != "APPLICABLE":
        what = (f"{hook_files} hook markdown file(s), {len(refs)} reference(s) checked"
                if hook_files else "no markdown anywhere")
        print(f"  census NOT-APPLICABLE: the plugin has no agents, commands, skills or resource markdown ({what})")
    elif not census_refs:
        print("static pass FAILED: no references found in agents, commands, skills or resources; "
              "a census of nothing proves nothing")
        bad = True
    for r in sorted(allowed, key=lambda r: (r.cls, r.rel_file, r.line)):
        print(f"  ALLOWED {r.cls:<14} {r.rel_file}:{r.line}  {r.raw}  [{r.allowed}]")
    for r in sorted(failing, key=lambda r: (r.cls, r.rel_file, r.line)):
        print(f"  {r.cls:<14} {r.rel_file}:{r.line}  {r.raw}")
    for line in stale:
        print(f"  {line}")
    print(f"static pass {'FAILED' if bad else 'clean'}: {len(failing)} failing reference(s)"
          + (f", {len(stale)} stale allowlist entr{'y' if len(stale) == 1 else 'ies'}" if stale else "")
          + (f", {len(allowed)} allowed" if allowed else ""))
    return 1 if bad else 0


# ---------------------------------------------------------------- targets ---

def read_meta(plugin: str) -> dict:
    with open(os.path.join(plugin, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
        return json.load(fh)


def plugin_name(plugin: str) -> str:
    return read_meta(plugin)["name"]


def _frontmatter_name(path: str) -> str | None:
    with open(path, encoding="utf-8") as fh:
        head = fh.read(4000)
    if not head.startswith("---"):
        return None
    m = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", head.split("---", 2)[1], re.M)
    return m.group(1) if m else None


def _frontmatter_true(path: str, key: str) -> bool:
    with open(path, encoding="utf-8") as fh:
        head = fh.read(4000)
    if not head.startswith("---"):
        return False
    m = re.search(rf"^{re.escape(key)}:\s*['\"]?(\w+)['\"]?\s*$", head.split("---", 2)[1], re.M)
    return bool(m and m.group(1).lower() == "true")


def _all_targets(plugin: str) -> list[tuple[str, str, bool]]:
    """(surface, name, loadable). A plugin without agents/ or skills/ has none of them."""
    out = []
    for surface in LIVE_SURFACES:
        root = os.path.join(plugin, surface)
        if not os.path.isdir(root):
            continue
        if surface == "agents":
            for f in sorted(os.listdir(root)):
                if f.endswith(".md"):
                    out.append((surface, _frontmatter_name(os.path.join(root, f)) or f[:-3], True))
        else:
            for d in sorted(os.listdir(root)):
                sk = os.path.join(root, d, "SKILL.md")
                if os.path.isfile(sk):
                    # the Skill tool refuses a disable-model-invocation skill ("Ask the user to
                    # run it themselves"), so no model run can load its text; the static pass
                    # still covers its references
                    out.append((surface, _frontmatter_name(sk) or d,
                                not _frontmatter_true(sk, "disable-model-invocation")))
    return out


def targets(plugin: str) -> list[tuple[str, str]]:
    """What the live pass can run: every agent and every skill the Skill tool will load."""
    return [(s, n) for s, n, ok in _all_targets(plugin) if ok]


def unloadable(plugin: str) -> list[tuple[str, str]]:
    return [(s, n) for s, n, ok in _all_targets(plugin) if not ok]


def _source_file(plugin: str, surface: str, name: str) -> str | None:
    root = os.path.join(plugin, surface)
    if not os.path.isdir(root):
        return None
    if surface == "agents":
        for f in sorted(os.listdir(root)):
            if f.endswith(".md") and (_frontmatter_name(os.path.join(root, f)) or f[:-3]) == name:
                return os.path.join("agents", f)
        return None
    for d in sorted(os.listdir(root)):
        sk = os.path.join(root, d, "SKILL.md")
        if os.path.isfile(sk) and (_frontmatter_name(sk) or d) == name:
            return os.path.join("skills", d, "SKILL.md")
    return None


# ------------------------------------------------------------------- live ---

def _is_nexus_checkout(d: str) -> bool:
    return (os.path.isfile(os.path.join(d, "conexus", ".claude-plugin", "plugin.json"))
            and os.path.isdir(os.path.join(d, "src", "nexus")))


def _detect_checkout() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(here, "..", "..", ".."))
    return root if _is_nexus_checkout(root) else None


def into_nexus_checkout(path: str, plugin: str) -> bool:
    """Outside the plugin, and inside a nexus checkout."""
    if _under(path, plugin):
        return False
    d = os.path.dirname(os.path.realpath(path))
    while True:
        if _is_nexus_checkout(d):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


@dataclass
class Run:
    surface: str
    name: str
    has_result: bool = False
    is_error: bool = False
    text: str = ""
    reads: dict = field(default_factory=dict)   # normalised file_path -> opened (bool; any success wins)
    tool_uses: int = 0
    basedir: str = "NONE"   # what Claude Code announced to a loaded skill


def parse_events(path: str) -> Run:
    base = os.path.basename(path)[: -len(".events.jsonl")]
    surface, name = base.split("__", 1)
    run = Run(surface, name)
    pending: dict[str, str] = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return run
    with fh:
        for line in fh:
            m = _BASEDIR.search(line)
            if m and run.basedir == "NONE":
                run.basedir = m.group(1).strip()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "result":
                run.has_result = True
                run.is_error = bool(e.get("is_error"))
                run.text = e.get("result") or ""
                continue
            msg = e.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    run.tool_uses += 1
                    if b.get("name") == "Read":
                        pending[b["id"]] = (b.get("input") or {}).get("file_path", "")
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    p = pending[b["tool_use_id"]]
                    p = canon(p)
                    run.reads[p] = run.reads.get(p, False) or not b.get("is_error")
    return run


def _clean_quoted(q: str) -> str:
    q = q.strip()
    m = _LINK.search(q)
    if m:
        q = m.group(1)
    return _strip(q)


def parse_ref_lines(text: str) -> list[tuple[str, str]]:
    """(quoted target, absolute path the model says it Read) for each REF line. The model was
    asked for tab separation; a pipe or runs of spaces are accepted too, and a bullet or
    backticks around the line."""
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•> ").strip("`").strip()
        if not line.startswith("REF"):
            continue
        rest = line[3:]
        if rest and rest[0] not in " \t|:":
            continue
        if "\t" in rest:
            fields = rest.split("\t")
        elif "|" in rest:
            fields = rest.split("|")
        else:
            fields = re.split(r"\s+", rest.strip())
        fields = [f.strip().strip("`").strip() for f in fields]
        fields = [f for f in fields if f and f != ":"]
        if len(fields) < 2:
            continue
        quoted = _clean_quoted(fields[0])
        if quoted:
            out.append((quoted, fields[-1]))
    return out


#: A verdict that is evidence of a plugin defect. Everything else short of OPENED is noise.
DEFINITE = frozenset({"FAILED", "UNSUBSTITUTED", "RELATIVE-DEAD", "INTO-CHECKOUT"})
NOISE_CLASSES = ("NO-REFS", "UNREAD", "MISRESOLVED", "NO-RESULT", "NO-EVENTS")


@dataclass
class RefVerdict:
    surface: str
    name: str
    quoted: str
    read_path: str
    verdict: str   # OPENED | a definite failure | a noise reason
    detail: str = ""
    mechanism: str = ""

    @property
    def definite(self) -> bool:
        return self.verdict in DEFINITE

    @property
    def opened(self) -> bool:
        return self.verdict == "OPENED"


def _mechanism(surface: str, cls: str) -> str:
    if surface == "agents":
        return "agent-variable"
    return "skill-variable" if cls == "RESOLVES" else "skill-relative"


def evaluate(run: Run, plugin: str, expected: dict[str, tuple[str, str]],
             dead: frozenset[str] = frozenset()) -> tuple[list[RefVerdict], list[RefVerdict]]:
    """-> (per-reference verdicts, run-level definite findings).

    expected: raw target -> (absolute path it opens, class); dead: the file's RELATIVE-DEAD targets.
    The Read events decide; the model's
    REF lines add what it QUOTED (a literal variable, a relative target in an agent) and are the
    fallback only for diagnosing why nothing was read."""
    refs = parse_ref_lines(run.text)
    by_raw: dict[str, tuple[str, str]] = {}
    for q, rp in refs:
        by_raw.setdefault(q, (q, rp))
        by_raw.setdefault(_BRACED.sub(plugin, q), (q, rp))
    verdicts: list[RefVerdict] = []
    for raw, (ep, cls) in sorted(expected.items()):
        ep = canon(ep)
        mech = _mechanism(run.surface, cls)

        def v(verdict: str, detail: str = "", quoted: str = raw, read: str = "") -> RefVerdict:
            return RefVerdict(run.surface, run.name, quoted, read, verdict, detail, mech)

        claim = by_raw.get(raw) or by_raw.get(_BRACED.sub(plugin, raw))
        quoted, read_path = claim if claim else (raw, "")
        if claim:
            if "CLAUDE_PLUGIN_ROOT" in quoted:
                verdicts.append(v("UNSUBSTITUTED", "the variable reached the model literally", quoted, read_path))
                continue
            if not quoted.startswith("/") and not _URL.match(quoted):
                if run.surface == "agents":
                    verdicts.append(v("RELATIVE-DEAD", "an agent's prompt carries no location to resolve it against",
                                      quoted, read_path))
                    continue
                if not run.basedir.startswith("/"):
                    verdicts.append(v("RELATIVE-DEAD", "no base directory announced to resolve it against",
                                      quoted, read_path))
                    continue
        if run.reads.get(ep) is True:
            verdicts.append(v("OPENED", "", quoted, read_path or ep))
        elif ep in run.reads and os.path.exists(ep):
            verdicts.append(v("FAILED", "the Read call returned an error for a path that exists", quoted, ep))
        elif claim:
            rp = canon(read_path) if read_path else ""
            if rp and rp != ep and run.reads.get(rp) is True:
                verdicts.append(v("MISRESOLVED", f"read {rp}, not {ep}", quoted, rp))
            else:
                verdicts.append(v("UNREAD", "no Read call for this path in the event stream", quoted, read_path))
        else:
            verdicts.append(v("NO-REFS", "no Read of it and no REF line for it", quoted, ""))
    if run.surface == "agents":
        for q, rp in refs:
            if q in dead:   # the model quoted a relative target from an agent's text: the text carries no location
                verdicts.append(RefVerdict(run.surface, run.name, q, rp, "RELATIVE-DEAD",
                                           "an agent's prompt carries no location to resolve it against",
                                           "agent-variable"))
    findings: list[RefVerdict] = []
    for p, ok in sorted(run.reads.items()):
        if "CLAUDE_PLUGIN_ROOT" in p:
            findings.append(RefVerdict(run.surface, run.name, p, p, "UNSUBSTITUTED",
                                       "a Read was given the variable literally"))
        elif ok and into_nexus_checkout(p, plugin):
            findings.append(RefVerdict(run.surface, run.name, p, p, "INTO-CHECKOUT",
                                       "opens, but outside the plugin and inside a nexus checkout"))
    return verdicts, findings


def target_state(run: Run | None, verdicts: list[RefVerdict], findings: list[RefVerdict], n_expected: int) -> str:
    """definite | ok | noise | none (nothing to probe)."""
    if run is None or not run.has_result or run.is_error:
        return "noise"
    if findings or any(x.definite for x in verdicts):
        return "definite"
    if n_expected == 0:
        return "none"
    return "ok" if any(x.opened for x in verdicts) else "noise"


def _expectations(plugin: str, static: list[Ref], rel: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for r in static:
        if r.rel_file != rel or r.cls not in ("RESOLVES", "RELATIVE-NEAR") or r.isdir or r.allowed:
            continue
        path = os.path.join(plugin, rel)
        opened = _target_path(r.raw, r.surface, path, plugin)
        if opened:
            out[r.raw] = (opened, r.cls)
    return out


def _live_report(plugin: str, events_dir: str, as_json: bool, partial: bool = False,
                 allow_path: str | None = None, retry_list: bool = False,
                 extra_known: frozenset[str] = frozenset()) -> int:
    allow, errors = load_allowlist(allow_path, _known_for(plugin, extra_known))
    if errors:
        for e in errors:
            print(f"  allowlist: {e}")
        print("live sweep FAILED: the allowlist is refused")
        return 1
    static = static_census(plugin, allow)
    names = sorted(f for f in os.listdir(events_dir) if f.endswith(".events.jsonl"))
    want = [f"{s}__{n}.events.jsonl" for s, n in targets(plugin)]
    runs: dict[str, Run] = {f: parse_events(os.path.join(events_dir, f)) for f in names}
    todo = names if partial else sorted(set(names) | set(want))

    states: dict[str, str] = {}
    detail: dict[str, tuple[list[RefVerdict], list[RefVerdict], int]] = {}
    for f in todo:
        run = runs.get(f)
        if run is None:
            states[f] = "noise"
            detail[f] = ([], [], 0)
            continue
        src = _source_file(plugin, run.surface, run.name)
        exp = _expectations(plugin, static, src) if src else {}
        dead = frozenset(r.raw for r in static if r.rel_file == src and r.cls == "RELATIVE-DEAD" and not r.allowed)
        vs, fs = evaluate(run, plugin, exp, dead) if run.has_result and not run.is_error else ([], [])
        states[f] = target_state(run, vs, fs, len(exp))
        detail[f] = (vs, fs, len(exp))

    if retry_list:
        for f in todo:
            if states[f] == "noise":
                print(f[: -len(".events.jsonl")])
        return 0

    print(f"live sweep: {len(names)} run(s) of {len(want)} targets" + (" (partial: --only)" if partial else ""))
    for s_, n_ in unloadable(plugin):
        print(f"  (not live-testable) {s_}/{n_}  disable-model-invocation: the Skill tool will not load it; the static pass covers it")
    failures = 0
    produced = 0
    opened = Counter()
    unprobed: list[str] = []
    refs_total = Counter()
    mech_present: Counter = Counter()
    rows = []
    for f in todo:
        label = f[: -len(".events.jsonl")].replace("__", "/", 1)
        run = runs.get(f)
        vs, fs, n_exp = detail[f]
        st = states[f]
        if run is not None and run.has_result and not run.is_error:
            produced += 1
        for x in vs:
            mech_present[x.mechanism] += 1
        for x in vs + fs:
            refs_total[x.verdict] += 1
            if x.opened:
                opened[x.mechanism] += 1
        if st == "definite":
            for x in vs:
                if x.definite:
                    print(f"  {x.verdict:<14} {label}  {x.quoted}  [{x.detail}]")
                    failures += 1
            for x in fs:
                print(f"  {x.verdict:<14} {label}  {x.quoted}  [{x.detail}]")
                failures += 1
        elif st == "noise":
            if run is None:
                why = "NO-EVENTS"
            elif not run.has_result or run.is_error:
                why = "NO-RESULT"
            else:
                why = "/".join(sorted({x.verdict for x in vs if not x.opened})) or "NO-REFS"
            unprobed.append(f"{label} ({why})")
            print(f"  UNPROBED       {label}  [{why}: no evidence either way after the retries]")
        rows.append({"run": label, "state": st, "refs": len(vs), "opened": sum(x.opened for x in vs),
                     "expected": n_exp, "tool_uses": run.tool_uses if run else 0})
    if as_json:
        print(json.dumps(rows, indent=1))
    print("  per-reference verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(refs_total.items())))
    n_ok = sum(1 for f in todo if states[f] == "ok")
    n_none = sum(1 for f in todo if states[f] == "none")
    print(f"  runs with a result: {produced}; targets opened: {n_ok}; UNPROBED: {len(unprobed)}; "
          f"nothing to probe: {n_none}; definite failures: {failures}")
    if failures:
        print(f"live sweep FAILED: {failures} definite failure(s)")
        return 1
    if produced == 0:
        print("live sweep UNVERIFIED: no run produced a result (claude unreachable or not signed in)")
        return 2
    if not partial:
        short = [m for m in sorted(mech_present) if opened[m] == 0]
        if short:
            print(f"live sweep UNVERIFIED: coverage floor not met, no OPENED reference for: {', '.join(short)}")
            return 2
    print(f"live sweep clean: {sum(opened.values())} reference(s) opened"
          + (f" (advisory; {len(unprobed)} target(s) UNPROBED)" if unprobed else ""))
    return 0


def main(argv: list[str]) -> int:
    global NEXUS_CHECKOUT
    p = argparse.ArgumentParser(prog="plugin_out_of_repo.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("static", "targets", "applicability", "meta", "live"):
        s = sub.add_parser(name)
        s.add_argument("--plugin", required=True)
        s.add_argument("--repo-root", default=None)
        s.add_argument("--known-plugin", action="append", default=[])
        if name in ("static", "live"):
            s.add_argument("--json", action="store_true")
            s.add_argument("--allowlist", default=None)
        if name == "live":
            s.add_argument("--events-dir", required=True)
            s.add_argument("--partial", action="store_true")
            s.add_argument("--retry-list", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 64
    NEXUS_CHECKOUT = os.path.realpath(a.repo_root) if a.repo_root else _detect_checkout()
    plugin = os.path.realpath(a.plugin)
    if not os.path.isfile(os.path.join(plugin, ".claude-plugin", "plugin.json")):
        print(f"not a plugin directory (no .claude-plugin/plugin.json): {plugin}", file=sys.stderr)
        return 64
    extra = frozenset(a.known_plugin)
    if a.cmd == "static":
        return _static_report(plugin, a.json, a.allowlist, extra)
    if a.cmd == "targets":
        for surface, name in targets(plugin):
            print(f"{surface}\t{plugin_name(plugin)}:{name}\t{name}")
        return 0
    if a.cmd == "applicability":
        kind, census, hooks = applicability(plugin)
        print(f"{kind}\t{census}\t{hooks}")
        return 0
    if a.cmd == "meta":
        meta = read_meta(plugin)
        print(f"{meta.get('name', '')}\t{meta.get('version', '')}")
        return 0
    return _live_report(plugin, a.events_dir, a.json, a.partial, a.allowlist, a.retry_list, extra)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
