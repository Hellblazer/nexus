# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host side of tests/e2e/plugin-out-of-repo-gate.sh (nexus-z7ddd).

The conexus plugin is checked everywhere from inside the nexus checkout, where
`../resources/agent-shared/...`, `conexus/registry.yaml` and `scripts/...` all
happen to open. A user runs it from a project that has none of those. This
module holds the two judgements the gate makes, with no model call in either:

  static   Every file reference in agents/, commands/, skills/ and resources/
           markdown, after substituting ${CLAUDE_PLUGIN_ROOT}, classified:

             RESOLVES       an absolute path (after substitution) inside the
                            plugin that exists
             RELATIVE-NEAR  a relative path that opens from the containing
                            file's own directory, in a skill or a resource
                            (the consumer learns that directory: a skill is
                            loaded with "Base directory for this skill", a
                            resource is Read by absolute path). Not a failure.
             REPO-ONLY      a path that exists only in the nexus repository
                            (conexus/..., src/..., tests/..., scripts/...)
             RELATIVE-DEAD  a relative path with no directory the consumer
                            knows: any relative path in an agent or command
                            (their text carries no location), or one that
                            opens from nowhere
             MISSING        an absolute path that does not exist
             OUTSIDE        an absolute path outside the plugin
             URL            a URL or catalog scheme; not a file
             PLACEHOLDER    a glob, a $VAR, a <name>, a ~ path, NNN: not a file
             PROJECT        a path in the user's own project (docs/rdr/...)
             UNSUBSTITUTED  a ${CLAUDE_PLUGIN_ROOT} path in hook-injected
                            markdown: Claude Code substitutes the variable in
                            agent and skill text, not in what a hook prints, so
                            the model would read it literally

           A directory counts as a file: a reference to a directory that exists
           (for example ${CLAUDE_PLUGIN_ROOT}/skills/prose-edit/scripts/) is
           RESOLVES, or RELATIVE-NEAR from a skill or resource.

           REPO-ONLY, RELATIVE-DEAD, MISSING, OUTSIDE and UNSUBSTITUTED fail the
           gate, unless the reference has an entry in the allowlist.

           Surfaces: agents, commands, skills and resources (the census), and
           hooks: every .md file under hooks/ except README.md, which are the
           files a hook script reads and prints into the model's context (the sn
           *-section.md files). A hook-injected file has no location, so it is
           judged like an agent: a relative path is RELATIVE-DEAD.

           NOT-APPLICABLE. A plugin with no agents, commands, skills or resource
           markdown (sn, conexus-tool-spill) has no census to take. It reports
           NOT-APPLICABLE for that census instead of failing as "a census of
           nothing"; its hook markdown still gets the static pass. A plugin that
           HAS agents, commands, skills or resources and yields no reference in
           them still fails (the nexus-moht0 non-vacuity rule).

  allowlist  A checked-in file (tests/e2e/plugin-out-of-repo-allowlist.txt),
           one entry per line:  <plugin> | <plugin-relative file> | <target as
           written> | <reason>.  No line number: the key is the file and the
           target. The reason must say why the reference is acceptable. The file
           is refused when a line is malformed, its reason is empty, or a key is
           repeated; an entry for this plugin is STALE, and fails, when its file
           or target is no longer in the plugin, or the reference no longer
           fails. The shipped file is empty: an entry is a decision, not a habit.

  live     The stream-json events of one `claude -p` run per agent and per
           skill: the model quotes each file reference in the prompt it was
           given and Reads it. Judged on the Read tool results in the event
           stream, not on the model's word. A quoted target that is relative in
           an agent fails whether or not the model guessed a directory that
           opened; one that opens outside the plugin and inside a nexus
           checkout fails; a run that quoted nothing where the static pass
           expects references fails.

Usage:
  plugin_out_of_repo.py static  --plugin DIR [--json] [--allowlist FILE]
  plugin_out_of_repo.py targets --plugin DIR
  plugin_out_of_repo.py applicability --plugin DIR
  plugin_out_of_repo.py live    --plugin DIR --events-dir DIR [--json] [--allowlist FILE]

Exit: 0 clean, 1 a failure, 2 the live pass could not run at all (no run
produced a result), 64 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

CENSUS_SURFACES = ("agents", "commands", "skills", "resources")
SURFACES = CENSUS_SURFACES + ("hooks",)
LIVE_SURFACES = ("agents", "skills")

FAILING = frozenset({"REPO-ONLY", "RELATIVE-DEAD", "MISSING", "OUTSIDE", "UNSUBSTITUTED"})
CLASSES = (
    "RESOLVES", "RELATIVE-NEAR", "REPO-ONLY", "RELATIVE-DEAD", "MISSING",
    "OUTSIDE", "UNSUBSTITUTED", "URL", "PLACEHOLDER", "PROJECT",
)
NON_FILE = frozenset({"URL", "PLACEHOLDER", "PROJECT"})

# First path segments that exist in the nexus repository and in no installed
# plugin. `docs/rdr/` is carved out below: the RDR skills name the user's own
# project directory with it.
NEXUS_ONLY_ROOTS = frozenset({
    "conexus", "src", "tests", "scripts", "service", "mcpb", "sn", "tool-spill",
    "prose-edit", "web", "docs", ".claude", ".claude-plugin", ".github",
})
PROJECT_ROOTS = frozenset({".serena", ".beads", ".git"})
EXTS = ("md", "yaml", "yml", "json", "sh", "py", "txt", "toml", "xml", "java", "cfg")

_ROOT_VAR = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b")
_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_ROOT_PATH = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/[A-Za-z0-9_./\-]+")
_TICK = re.compile(r"`([^`\n]+)`")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://|^mailto:", re.I)
_PATHISH = re.compile(r"^[~.$A-Za-z0-9_{}<>*\-/.:#@\[\]]+$")
_BASEDIR = re.compile(r"Base directory for this skill: ([^\\\"\n]+)")
_LINESUF = re.compile(r":\d+(?:-\d+)?$")


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


def extract(text: str):
    """Yield (line number, target as written) for every file-ish reference."""
    for no, line in enumerate(text.splitlines(), 1):
        seen: set[str] = set()
        found: list[str] = []
        for m in _LINK.finditer(line):
            found.append(m.group(1))
        for m in _ROOT_PATH.finditer(line):
            found.append(m.group(0))
        for m in _TICK.finditer(line):
            t = m.group(1).strip()
            if " " in t or "/" not in t or not _PATHISH.match(t):
                continue
            base = _strip(t)
            if base.rsplit(".", 1)[-1] not in EXTS or "." not in base.rsplit("/", 1)[-1]:
                continue
            found.append(t)
        for raw in found:
            t = _strip(raw)
            if not t or t.startswith("#") or t in seen:
                continue
            seen.add(t)
            yield no, t


def _under(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _exists(path: str) -> bool:
    """A file or a directory: a reference may name either."""
    return os.path.isfile(path) or os.path.isdir(path)


def _target_path(target: str, surface: str, file_abs: str, plugin: str) -> str | None:
    """The absolute path a reference opens, or None when it opens nothing."""
    sub = _ROOT_VAR.sub(plugin, target)
    if sub.startswith("/"):
        return sub if _exists(sub) else None
    if surface in ("skills", "resources"):
        near = os.path.normpath(os.path.join(os.path.dirname(file_abs), sub))
        if _exists(near) and _under(near, plugin):
            return near
    return None


def classify(target: str, surface: str, file_abs: str, plugin: str) -> str:
    if _URL.match(target):
        return "URL"
    if surface == "hooks" and _ROOT_VAR.search(target):
        return "UNSUBSTITUTED"
    sub = _ROOT_VAR.sub(plugin, target)
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
    if first in NEXUS_ONLY_ROOTS:
        return "REPO-ONLY"
    if surface in ("skills", "resources"):
        near = os.path.normpath(os.path.join(os.path.dirname(file_abs), sub))
        if _exists(near) and _under(near, plugin):
            return "RELATIVE-NEAR"
    return "RELATIVE-DEAD"


MIN_REASON = 12   # characters: a sentence, not a placeholder


def load_allowlist(path: str | None) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """-> ({(plugin, file, target): reason}, errors). A bad line is an error, never skipped."""
    allow: dict[tuple[str, str, str], str] = {}
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
            parts = [p.strip() for p in line.split("|", 3)]
            where = f"{os.path.basename(path)}:{no}"
            if len(parts) != 4:
                errors.append(f"{where}: want '<plugin> | <file> | <target> | <reason>', got {len(parts)} field(s)")
                continue
            plugin, rel, target, reason = parts
            if not (plugin and rel and target):
                errors.append(f"{where}: plugin, file and target must all be given")
                continue
            if len(reason) < MIN_REASON:
                errors.append(f"{where}: the reason is {'empty' if not reason else 'too short'} "
                              f"(write why {target} is acceptable, at least {MIN_REASON} characters)")
                continue
            key = (plugin, rel, target)
            if key in allow:
                errors.append(f"{where}: repeats an earlier entry for {rel} {target}")
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
            reason = allow.get((name, rel, target), "") if cls in FAILING else ""
            refs.append(Ref(surface, rel, no, target, cls,
                            bool(opened and os.path.isdir(opened)), reason))
    return refs


def stale_entries(plugin: str, refs: list[Ref], allow: dict) -> list[str]:
    """Allowlist entries for this plugin that no longer cover a failing reference."""
    name = plugin_name(plugin)
    files = {os.path.relpath(p, plugin) for _s, p in plugin_files(plugin)}
    out = []
    for (pl, rel, target), _reason in sorted(allow.items()):
        if pl != name:
            continue
        here = [r for r in refs if r.rel_file == rel and r.raw == target]
        if rel not in files:
            out.append(f"STALE {rel}  {target}  [the file is no longer in the plugin]")
        elif not here:
            out.append(f"STALE {rel}  {target}  [the target is no longer in the file]")
        elif not any(r.cls in FAILING for r in here):
            out.append(f"STALE {rel}  {target}  [the reference no longer fails (it is {here[0].cls}); drop the entry]")
    return out


def applicability(plugin: str) -> tuple[str, int, int]:
    """-> (kind, census files, hook files). kind: APPLICABLE | HOOKS-ONLY | NO-MARKDOWN."""
    census = sum(1 for s, _ in plugin_files(plugin) if s in CENSUS_SURFACES)
    hooks = sum(1 for s, _ in plugin_files(plugin) if s == "hooks")
    kind = "APPLICABLE" if census else ("HOOKS-ONLY" if hooks else "NO-MARKDOWN")
    return kind, census, hooks


def _static_report(plugin: str, as_json: bool, allow_path: str | None = None) -> int:
    allow, errors = load_allowlist(allow_path)
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

def plugin_name(plugin: str) -> str:
    with open(os.path.join(plugin, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
        return json.load(fh)["name"]


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
    reads: dict = field(default_factory=dict)   # file_path -> opened (bool)
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
                    run.reads[pending[b["tool_use_id"]]] = not b.get("is_error")
    return run


def _clean_quoted(q: str) -> str:
    q = q.strip()
    m = _LINK.search(q)
    if m:
        q = m.group(1)
    return _strip(q)


@dataclass
class RefVerdict:
    surface: str
    name: str
    quoted: str
    read_path: str
    verdict: str   # OPENED or a failure reason
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "OPENED"


def judge(run: Run, plugin: str, expected: set[str]) -> tuple[list[RefVerdict], list[str], list[str], str]:
    """-> (per-reference verdicts, missed, extra, basedir)."""
    verdicts: list[RefVerdict] = []
    basedir = run.basedir
    reported: set[str] = set()
    for raw in run.text.splitlines():
        parts = [p.strip().strip("`") for p in raw.strip().lstrip("-*• ").split("\t")]
        if not parts or not parts[0]:
            continue
        if parts[0] != "REF" or len(parts) < 3:
            continue
        quoted, read_path = _clean_quoted(parts[1]), parts[2]
        if not quoted:
            continue
        reported.add(_ROOT_VAR.sub(plugin, quoted))
        v = _judge_ref(run, plugin, quoted, read_path, basedir)
        verdicts.append(v)
    exp = {_ROOT_VAR.sub(plugin, e) for e in expected}
    return verdicts, sorted(exp - reported), sorted(reported - exp), basedir


def _judge_ref(run: Run, plugin: str, quoted: str, read_path: str, basedir: str) -> RefVerdict:
    def v(verdict: str, detail: str = "") -> RefVerdict:
        return RefVerdict(run.surface, run.name, quoted, read_path, verdict, detail)

    if _URL.match(quoted):
        return v("OPENED", "URL, not a file")
    if "CLAUDE_PLUGIN_ROOT" in quoted:
        return v("UNSUBSTITUTED", "the variable reached the model literally")
    if not quoted.startswith("/"):
        if run.surface == "agents":
            return v("RELATIVE-DEAD", "an agent's prompt carries no location to resolve it against")
        if not basedir.startswith("/"):
            return v("RELATIVE-DEAD", "no base directory announced to resolve it against")
        want = os.path.normpath(os.path.join(basedir, quoted))
        if os.path.normpath(read_path) != want:
            return v("MISRESOLVED", f"base directory + target is {want}")
    elif os.path.normpath(read_path) != os.path.normpath(quoted):
        return v("MISRESOLVED", "the path read is not the path quoted")
    if read_path not in run.reads:
        return v("UNREAD", "no Read call for this path in the event stream")
    if not run.reads[read_path]:
        return v("FAILED", "the Read call returned an error")
    if into_nexus_checkout(read_path, plugin):
        return v("INTO-CHECKOUT", "opens, but outside the plugin and inside a nexus checkout")
    return v("OPENED")


def _live_report(plugin: str, events_dir: str, as_json: bool, partial: bool = False,
                 allow_path: str | None = None) -> int:
    allow, errors = load_allowlist(allow_path)
    if errors:
        for e in errors:
            print(f"  allowlist: {e}")
        print("live pass FAILED: the allowlist is refused")
        return 1
    static = static_census(plugin, allow)
    expect_for: dict[str, set[str]] = defaultdict(set)
    for r in static:
        # A directory is not a Read target (the Read tool refuses one); an allowed
        # reference is a recorded decision, not an expectation.
        if r.cls not in NON_FILE and not r.isdir and not r.allowed:
            expect_for[r.rel_file].add(r.raw)
    names = sorted(f for f in os.listdir(events_dir) if f.endswith(".events.jsonl"))
    want = {f"{s}__{n}.events.jsonl" for s, n in targets(plugin)}
    rows = []
    failures = 0
    produced = 0
    refs_total = Counter()
    print(f"live pass: {len(names)} run(s) of {len(want)} targets" + (" (partial: --only)" if partial else ""))
    for s_, n_ in unloadable(plugin):
        print(f"  (not live-testable) {s_}/{n_}  disable-model-invocation: the Skill tool will not load it; the static pass covers it")
    for missing in sorted(set() if partial else want - set(names)):
        print(f"  NO-EVENTS    {missing[:-len('.events.jsonl')]}")
        failures += 1
    for f in names:
        run = parse_events(os.path.join(events_dir, f))
        label = f"{run.surface}/{run.name}"
        if not run.has_result or run.is_error:
            print(f"  NO-RESULT    {label}  ({'error result' if run.has_result else 'no result event'})")
            failures += 1
            continue
        produced += 1
        src = _source_file(plugin, run.surface, run.name)
        expected = expect_for.get(src or "", set())
        verdicts, missed, extra, basedir = judge(run, plugin, expected)
        exp_norm = {_ROOT_VAR.sub(plugin, e) for e in expected}
        # A reference the plugin's markdown does not contain (the model also
        # read hook-injected text, CLAUDE.md, the skill list) is reported as
        # (extra) and never fails the gate: it is not the plugin's to fix here.
        judged = [x for x in verdicts if _ROOT_VAR.sub(plugin, x.quoted) in exp_norm]
        bad = [x for x in judged if not x.ok]
        if expected and not judged:
            print(f"  NO-REFS        {label}  (static pass expects {len(expected)}, the run quoted none of them)")
            failures += 1
        for x in judged:
            refs_total[x.verdict] += 1
        for x in bad:
            print(f"  {x.verdict:<14} {label}  {x.quoted}  [{x.detail}]")
            failures += 1
        for m in missed:
            print(f"  (missed)       {label}  {os.path.relpath(m, plugin) if m.startswith(plugin) else m}")
        for m in extra:
            print(f"  (extra)        {label}  {m}")
        rows.append({
            "run": label, "refs": len(judged), "opened": len(judged) - len(bad),
            "failing": len(bad), "expected": len(expected), "missed": len(missed),
            "extra": len(extra), "basedir": basedir, "tool_uses": run.tool_uses,
        })
    if as_json:
        print(json.dumps(rows, indent=1))
    print(f"  per-reference verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(refs_total.items())))
    print(f"  runs with a result: {produced}; references judged: {sum(refs_total.values())}; "
          f"expected by the static pass: {sum(len(expect_for.get(_source_file(plugin, s, n) or '', ())) for s, n in targets(plugin))}")
    if produced == 0:
        print("live pass UNVERIFIED: no run produced a result (claude unreachable or not signed in)")
        return 2
    print(f"live pass {'FAILED' if failures else 'clean'}: {failures} failure(s)")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="plugin_out_of_repo.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("static", "targets", "applicability", "live"):
        s = sub.add_parser(name)
        s.add_argument("--plugin", required=True)
        if name in ("static", "live"):
            s.add_argument("--json", action="store_true")
            s.add_argument("--allowlist", default=None)
        if name == "live":
            s.add_argument("--events-dir", required=True)
            s.add_argument("--partial", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 64
    plugin = os.path.realpath(a.plugin)
    if not os.path.isfile(os.path.join(plugin, ".claude-plugin", "plugin.json")):
        print(f"not a plugin directory (no .claude-plugin/plugin.json): {plugin}", file=sys.stderr)
        return 64
    if a.cmd == "static":
        return _static_report(plugin, a.json, a.allowlist)
    if a.cmd == "targets":
        for surface, name in targets(plugin):
            print(f"{surface}\t{plugin_name(plugin)}:{name}\t{name}")
        return 0
    if a.cmd == "applicability":
        kind, census, hooks = applicability(plugin)
        print(f"{kind}\t{census}\t{hooks}")
        return 0
    return _live_report(plugin, a.events_dir, a.json, a.partial, a.allowlist)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
