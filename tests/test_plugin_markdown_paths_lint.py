# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin skill, command and agent markdown names plugin files by a path an
agent can open.

On 2026-10-27 a gate critic was briefed "see resources/rdr/REGISTER.md",
could not find it under the worktree, and ran ``find /`` over the whole disk
(nexus-egv6q). The rdr-gate-checklist skill wrote the path that way, and the
orchestrator copied it into the brief. Spellings that reach an agent
unresolved:

- a bare ``resources/...`` path: it has no anchor outside the plugin;
- an unbraced ``$CLAUDE_PLUGIN_ROOT``: Claude Code substitutes only the
  braced ``${CLAUDE_PLUGIN_ROOT}`` in plugin markdown, and the variable is
  not set in a session's Bash, so the unbraced form stays literal;
- ``$RDR_DIR`` or ``${RDR_DIR}`` in a relay or brief: the orchestrator copies
  the line into the agent's prompt and the variable arrives unexpanded
  (nexus-3811j). The skills write ``<RDR dir>``, defined once as the absolute
  path;
- a bare plugin file name (``registry.yaml``) in an agent or command body;
- a nexus repo path an agent in another project cannot open: ``docs/*.md``
  (``docs/rdr/`` is the project's own), and ``tests/``, ``scripts/``,
  ``src/nexus/``, ``service/`` in a skill, agent or command (nexus-4deer).
  Deliberate sites are in ``_REPO_PATH_ALLOWED``, each with its reason.

Allowed: ``${CLAUDE_PLUGIN_ROOT}/resources/...`` (rendered as an absolute
path), and in skills only a ``../`` link target: Claude Code shows a loaded
skill with its base directory, so the reader can resolve it. An agent or
command body arrives as a prompt with no base directory, so a ``../`` target
there is unanchored too (review r1 of nexus-egv6q: 38 such links).

File-only, so it runs in the ``-m lint`` job. Planted fixtures prove each
rule fires (nexus-moht0), a tmp tree proves the live scan applies every rule,
and the live check prints what it scanned.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
CONEXUS_DIR = REPO_ROOT / "conexus"
SKILLS_DIR = CONEXUS_DIR / "skills"
PLUGIN_MD_DIRS = tuple(CONEXUS_DIR / d for d in ("skills", "commands", "agents"))
#: Plugin docs an agent is sent to read; they carry snippets of agent text. An
#: agent opens them by path, so a relative link between them resolves.
SHARED_DOCS_DIR = CONEXUS_DIR / "resources" / "agent-shared"

#: ``resources/`` not preceded by ``/`` (``${CLAUDE_PLUGIN_ROOT}/``, ``../``),
#: a word character (``conexus/resources`` is fine as a repo path too, it has
#: a slash) or ``}``/``.``.
_BARE_RESOURCES = re.compile(r"(?<![/\w}.])(?:\./)?resources/[\w./-]+")
_UNBRACED_ROOT = re.compile(r"\$CLAUDE_PLUGIN_ROOT\b")
_RELATIVE_TARGET = re.compile(r"\]\((\.\.?/[^)]*)\)")

#: ``$RDR_DIR`` and ``${RDR_DIR}``: a dispatched agent cannot expand either.
_RDR_DIR = re.compile(r"\$RDR_DIR\b|\$\{RDR_DIR\}")
#: A line that opens a relay: its ``**Task**`` line or a ``## Relay`` heading.
_RELAY_MARK = re.compile(r"\*\*Task\*\*|^\s*#+\s*Relay\b", re.IGNORECASE)
#: A line in a relay or brief context. No exemption by keyword: the fix is to
#: write ``<RDR dir>``, never a variable plus a note about it (nexus-3811j).
_BRIEF_LINE = re.compile(r"\*\*Task\*\*|^\s*-\s*Files:|^\s*#+\s*Relay\b|\bbrief\b", re.IGNORECASE)

#: A nexus repo doc (``docs/collections.md``) named in plugin text, also as
#: ``./docs/x.md`` or ``../docs/x.md``: an agent in another project has no such
#: file, so the text links it on GitHub or states the rule inline (nexus-4deer).
#: Only ``docs/rdr/`` is the project's own directory; ``docs/rdr-template.md`` is not.
_REPO_DOC = re.compile(r"(?<![\w/.:-])(?:\.{1,2}/)*docs/(?!rdr/)[\w./-]+\.(?:md|txt|ya?ml)")
#: A nexus repo path other than ``docs/`` named in a skill, agent or command.
#: Generic example paths (``src/main/java/...``) are outside the pattern.
_REPO_PATH = re.compile(
    r"(?<![\w/.:-])(?:\.{1,2}/)*(?:tests|scripts|src/nexus|service|\.claude/rules)/[\w./*-]*[\w*-]"
)

#: Deliberate repo paths: file (from the repo root) -> {path: why it stays}.
#: Add an entry only with a reason an agent in another project is not misled.
_REPO_PATH_ALLOWED: dict[str, dict[str, str]] = {
    "conexus/skills/writing-nx-skills/SKILL.md": {
        path: "maintainer skill: it is how nexus's own plugin skills are written, read in this repo"
        for path in (
            "tests/test_plugin_structure.py",
            "src/nexus/session_start_guidance.py",
            "tests/test_session_start_guidance.py",
            "tests/hooks/test_session_start_combined_budget.py",
        )
    },
    "conexus/skills/catalog/SKILL.md": {
        "src/nexus/scoring.py": "an example file path to look up in a catalog; any indexed file would do",
    },
    "conexus/skills/composition-probe/SKILL.md": {
        "src/nexus/search_engine.py": "historical citation of the spike that verified the tool budget",
        "scripts/mvnw-leased.sh": "named under 'when the project has that script' (the nexus repo)",
        "service/mvnw": "named inside the same nexus-repo clause, as what the script wraps",
    },
    "conexus/skills/orchestration/SKILL.md": {
        ".claude/rules/releases-and-engine.md": "labelled 'this repo's own' file, not a path the reader must open",
    },
    "conexus/skills/cli-controller/SKILL.md": {
        "service/target": "inside an HTML comment saying the example is generic and the path is nexus's",
    },
    "conexus/commands/continuation.md": {
        "tests/e2e/lib/commit_scope_audit.sh": "guarded: 'when the project has' the script, with a by-hand fallback",
    },
}

#: Plugin file names an agent or command body might name bare (``registry.yaml``, ``reference.md``): with no base
#: directory a bare name is unanchored. Common names any project has are left out.
_COMMON_NAMES = frozenset({"README.md", "SKILL.md", "CHANGELOG.md", "LICENSE", "plugin.json"})


def _plugin_file_names(root: Path = CONEXUS_DIR) -> frozenset[str]:
    """File names under ``resources/`` and ``skills/`` and at the top of ``root`` (``registry.yaml``)."""
    files = [p for d in ("resources", "skills") for p in (root / d).rglob("*") if p.is_file()]
    files += [p for p in root.iterdir() if p.is_file()]
    return frozenset(p.name for p in files if "." in p.name and p.name not in _COMMON_NAMES)


def _bare_name_pattern(names: frozenset[str]) -> re.Pattern[str]:
    alts = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    return re.compile(rf"(?<![/\w.$}}\[-])(?:{alts})(?![\w-])")  # a link label `[NAME](...)` is anchored by its target


def _relay_block_lines(lines: list[str]) -> set[int]:
    """0-based indexes of the lines inside a fenced block that holds a relay template."""
    found: set[int] = set()
    start: int | None = None
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("```"):
            continue
        if start is None:
            start = i
        else:
            if any(_RELAY_MARK.search(lines[j]) for j in range(start, i + 1)):
                found.update(range(start, i + 1))
            start = None
    return found


def _violations(
    text: str,
    *,
    relative_links_ok: bool = True,
    bare_names: re.Pattern[str] | None = None,
    repo_path_allow: frozenset[str] | None = None,
) -> list[str]:
    """``repo_path_allow`` None turns the repo-path rule off; a set turns it on, minus those paths."""
    found = []
    lines = text.splitlines()
    in_relay_block = _relay_block_lines(lines)
    for lineno, line in enumerate(lines, 1):
        if not relative_links_ok:
            for m in _RELATIVE_TARGET.finditer(line):
                found.append(f"{lineno}: relative link {m.group(1)!r} in a prompt with no base directory")
            if bare_names is not None:
                for m in bare_names.finditer(line):
                    found.append(f"{lineno}: bare plugin file name {m.group(0)!r} in a prompt with no base directory")
        for m in _REPO_DOC.finditer(line):
            found.append(f"{lineno}: repo-only doc {m.group(0)!r} (link it on GitHub or state the rule inline)")
        if repo_path_allow is not None:
            for m in _REPO_PATH.finditer(line):
                if m.group(0) not in repo_path_allow:
                    found.append(f"{lineno}: repo-only path {m.group(0)!r} (guard it, link it, or add it to _REPO_PATH_ALLOWED)")
        if _RDR_DIR.search(line) and (lineno - 1 in in_relay_block or _BRIEF_LINE.search(line)):
            found.append(f"{lineno}: $RDR_DIR in a relay or brief line (write <RDR dir>, the absolute path)")
        for m in _BARE_RESOURCES.finditer(line):
            found.append(f"{lineno}: bare plugin path {m.group(0)!r}")
        for m in _UNBRACED_ROOT.finditer(line):
            found.append(f"{lineno}: unbraced {m.group(0)!r} (write ${{CLAUDE_PLUGIN_ROOT}})")
    return found


def _scan(
    files: list[Path],
    *,
    skills_dir: Path,
    shared_dir: Path,
    rel_base: Path,
    bare_names: re.Pattern[str],
    repo_path_allowed: dict[str, dict[str, str]],
) -> tuple[list[str], int, int]:
    """Apply every rule to each file; return (violations, resources/-or-root mentions, files checked for relative links)."""
    mentions = relative_checked = 0
    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        mentions += text.count("resources/") + text.count("CLAUDE_PLUGIN_ROOT")
        in_shared = shared_dir in path.parents
        has_base_dir = skills_dir in path.parents or in_shared
        relative_checked += 0 if has_base_dir else 1
        rel = path.relative_to(rel_base).as_posix()
        # Shared docs carry maintainer notes that name repo paths on purpose; the rule skips them.
        allow = None if in_shared else frozenset(repo_path_allowed.get(rel, {}))
        bad += [
            f"{rel}:{v}"
            for v in _violations(text, relative_links_ok=has_base_dir, bare_names=bare_names, repo_path_allow=allow)
        ]
    return bad, mentions, relative_checked


def _scan_live(
    dirs: tuple[Path, ...] = PLUGIN_MD_DIRS,
    shared_dir: Path = SHARED_DOCS_DIR,
    skills_dir: Path = SKILLS_DIR,
    rel_base: Path = REPO_ROOT,
) -> tuple[list[Path], list[str], int, int]:
    """The live wiring: the real name census and the real allowlist over the given roots."""
    files = sorted(p for d in (*dirs, shared_dir) if d.is_dir() for p in d.rglob("*.md"))
    bad, mentions, relative_checked = _scan(
        files,
        skills_dir=skills_dir,
        shared_dir=shared_dir,
        rel_base=rel_base,
        bare_names=_bare_name_pattern(_plugin_file_names()),
        repo_path_allowed=_REPO_PATH_ALLOWED,
    )
    return files, bad, mentions, relative_checked


@pytest.mark.parametrize(
    "text",
    [
        "Brief the critic: see resources/rdr/REGISTER.md.",
        "Full checklist: [resources/tier-discipline.md](../../resources/tier-discipline.md)",
        "Copy from `$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md`.",
        "Copy from `./resources/rdr/TEMPLATE.md`.",
        "See `/conexus:plan-first` and `docs/plan-authoring-guide.md`.",
        "Per docs/collections.md Rule 1, a collection is a subject.",
    ],
)
def test_planted_bad_path_is_flagged(text: str) -> None:
    assert _violations(text)


@pytest.mark.parametrize(
    "text",
    [
        "- Files: $RDR_DIR/NNN-*.md",
        "- Files: ${RDR_DIR}/NNN-*.md",
        "**Task**: Fix check: verify ONLY `git diff <gated>..HEAD -- $RDR_DIR/NNN-*.md`.",
        "**Task**: Fix check: verify ONLY `git diff <gated>..HEAD -- ${RDR_DIR}/NNN-*.md`.",
        "The dispatch brief includes the register (`$RDR_DIR/REGISTER.md`).",
        # No keyword exempts a line: a note about the variable next to it still lets it through.
        "The dispatch brief includes the register (absolute `$RDR_DIR/REGISTER.md`).",
        "## Relay: substantive-critic $RDR_DIR",
        "```markdown\n## Relay: critic\n\nRead $RDR_DIR/NNN.md first.\n```",
        "```\n**Task**: x\nsome plain line ${RDR_DIR}/y\n```",
    ],
)
def test_rdr_dir_in_a_relay_or_brief_is_flagged(text: str) -> None:
    assert any("RDR_DIR" in v for v in _violations(text)), text


@pytest.mark.parametrize(
    "text",
    [
        "All file paths below use `$RDR_DIR` in place of `docs/rdr`.",
        "`<RDR dir>` is the absolute path of the RDR directory: `$RDR_DIR` joined to the repository root.",
        "Write it out in any brief or relay: a dispatched agent cannot expand the variable.",
        "- Files: <RDR dir>/NNN-*.md and the diff range above",
        "**Task**: verify `git diff <gated>..HEAD -- <RDR dir>/NNN-*.md`.",
        "Prose register: `$RDR_DIR/REGISTER.md`, falling back to the plugin copy.",
        # a fenced block without a relay marker is shell, not a prompt
        "```bash\ngit add $RDR_DIR/NNN-kebab-title.md\n```",
        # a relay block that ends before the line
        "```markdown\n## Relay: x\n```\n\nRun `ls $RDR_DIR`.",
    ],
)
def test_rdr_dir_outside_a_relay_or_brief_passes(text: str) -> None:
    assert not _violations(text), text


def test_relative_link_is_flagged_outside_skills_only() -> None:
    text = "Follow the [protocol](../resources/agent-shared/CONTEXT_PROTOCOL.md)."
    assert _violations(text, relative_links_ok=False)
    assert not _violations(text, relative_links_ok=True)


@pytest.mark.parametrize(
    "text",
    [
        "Read `${CLAUDE_PLUGIN_ROOT}/resources/rdr/REGISTER.md`.",
        "See [RELAY_TEMPLATE.md](../../resources/agent-shared/RELAY_TEMPLATE.md).",
        "The repo file conexus/resources/rdr/TEMPLATE.md.",
    ],
)
def test_anchored_path_passes(text: str) -> None:
    assert not _violations(text)


@pytest.mark.parametrize(
    "text",
    [
        "Rules: docs/collections.md.",
        "Rules: ./docs/collections.md.",
        "Rules: ../docs/collections.md.",
        "Rules: ../../docs/collections.md.",
        "Template: docs/rdr-template.md.",
        "Schema: docs/schema.yaml and docs/notes.txt.",
    ],
)
def test_a_repo_doc_in_any_spelling_is_flagged(text: str) -> None:
    assert _violations(text), text


def test_a_github_link_or_the_projects_rdr_dir_passes() -> None:
    assert not _violations("Rules: https://github.com/Hellblazer/nexus/blob/main/docs/collections.md.")
    assert not _violations("Write the RDR to `docs/rdr/NNN-kebab-title.md`.")
    assert not _violations("Rules: https://github.com/Hellblazer/nexus/blob/main/docs/tuple-space.md.")


@pytest.mark.parametrize(
    "text",
    [
        "Run `bash tests/e2e/lib/commit_scope_audit.sh <ref>..`.",
        "Run scripts/mvnw-leased.sh test.",
        "Template `service/src/main/resources/tuples/templates/mailbox.yaml`.",
        "See src/nexus/search_engine.py:149.",
        "The `.claude/rules/releases-and-engine.md` file.",
        "Read ../tests/test_x.py.",
    ],
)
def test_a_repo_only_path_is_flagged_when_the_rule_is_on(text: str) -> None:
    assert _violations(text, repo_path_allow=frozenset()), text
    assert not _violations(text), "the rule is off without an allow set (shared docs)"


def test_a_repo_path_passes_when_linked_or_allowed() -> None:
    link = "Template: https://github.com/Hellblazer/nexus/blob/main/service/src/main/resources/tuples/templates/mailbox.yaml"
    assert not _violations(link, repo_path_allow=frozenset())
    assert not _violations("Run `tests/x.sh`.", repo_path_allow=frozenset({"tests/x.sh"}))
    assert _violations("Run `tests/x.sh` and `tests/y.sh`.", repo_path_allow=frozenset({"tests/x.sh"}))
    assert not _violations("A generic `src/main/java/auth/AuthController.java` example.", repo_path_allow=frozenset())


def test_a_bare_plugin_file_name_is_flagged_in_an_agent_body_only() -> None:
    names = _bare_name_pattern(frozenset({"registry.yaml", "reference.md"}))
    for text in ("Load registry.yaml first.", "See reference.md for the table."):
        assert _violations(text, relative_links_ok=False, bare_names=names), text
        assert not _violations(text, relative_links_ok=True, bare_names=names), text
    for ok in ("Read `${CLAUDE_PLUGIN_ROOT}/resources/registry.yaml`.", "the my-reference.md file", "a/reference.md",
               "[reference.md](${CLAUDE_PLUGIN_ROOT}/skills/x/reference.md)"):
        assert not _violations(ok, relative_links_ok=False, bare_names=names), ok


def test_the_plugin_file_name_census_holds_the_names_the_bead_cites() -> None:
    names = _plugin_file_names()
    assert {"registry.yaml", "reference.md"} <= names, sorted(names)


def test_the_census_leaves_out_the_common_names() -> None:
    on_disk = {p.name for p in CONEXUS_DIR.rglob("*") if p.is_file()}
    present = _COMMON_NAMES & on_disk
    assert {"SKILL.md", "README.md"} <= present, f"the tree no longer holds the names this pins: {sorted(present)}"
    assert not present & _plugin_file_names(), sorted(present & _plugin_file_names())
    assert _COMMON_NAMES >= {"README.md", "SKILL.md", "CHANGELOG.md"}


def test_the_census_reads_the_top_level_files_of_the_plugin_root(tmp_path: Path) -> None:
    (tmp_path / "resources").mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "top.yaml").write_text("x")
    (tmp_path / "skills" / "deep.md").write_text("x")
    assert _plugin_file_names(tmp_path) == {"top.yaml", "deep.md"}


def test_the_live_scan_applies_every_rule_to_an_agent_file(tmp_path: Path) -> None:
    """Dropping any rule from the live wiring (the name census, the allowlist, the repo-path rule) turns this red."""
    agents = tmp_path / "agents"
    agents.mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "shared").mkdir()
    (agents / "a.md").write_text(
        "Load registry.yaml first.\n"
        "Run tests/e2e/x.sh now.\n"
        "- Files: $RDR_DIR/NNN.md\n"
        "Rules: docs/collections.md.\n"
        "Copy resources/rdr/TEMPLATE.md.\n"
        "Set $CLAUDE_PLUGIN_ROOT.\n"
        "See [p](../resources/agent-shared/X.md).\n",
        encoding="utf-8",
    )
    _, bad, _, _ = _scan_live(dirs=(agents,), shared_dir=tmp_path / "shared", skills_dir=tmp_path / "skills", rel_base=tmp_path)
    joined = "\n".join(bad)
    for needle in ("registry.yaml", "tests/e2e/x.sh", "RDR_DIR", "docs/collections.md", "resources/rdr/TEMPLATE.md",
                   "CLAUDE_PLUGIN_ROOT", "../resources/agent-shared/X.md"):
        assert needle in joined, (needle, bad)


def test_the_live_scan_skips_repo_paths_in_shared_docs_and_honours_the_allowlist(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "agents").mkdir()
    (shared / "M.md").write_text("Maintainer note: scripts/x.sh and tests/y.py.\n", encoding="utf-8")
    _, bad, _, _ = _scan_live(dirs=(tmp_path / "agents",), shared_dir=shared, skills_dir=tmp_path / "skills", rel_base=tmp_path)
    assert not bad, bad


def test_every_allowlisted_repo_path_is_still_there() -> None:
    """A stale entry hides nothing today and a future use of the path tomorrow."""
    for rel, entries in _REPO_PATH_ALLOWED.items():
        path = REPO_ROOT / rel
        assert path.is_file(), rel
        assert any(d in path.parents for d in PLUGIN_MD_DIRS), rel
        found = {m.group(0) for line in path.read_text(encoding="utf-8").splitlines() for m in _REPO_PATH.finditer(line)}
        for token, why in entries.items():
            assert why.strip(), (rel, token)
            assert token in found, f"{rel}: allowlisted {token!r} is no longer in the file"


def test_plugin_markdown_names_plugin_files_by_an_openable_path() -> None:
    files, bad, mentions, relative_checked = _scan_live()
    print(f"relative-link rule applied to {relative_checked} agent and command files")
    assert relative_checked >= 20, f"only {relative_checked} agent and command files: wrong roots?"
    print(f"scanned {len(files)} files, {mentions} resources/ or plugin-root mentions, "
          f"{len(_plugin_file_names())} census names, {sum(map(len, _REPO_PATH_ALLOWED.values()))} allowlisted repo paths")
    assert len(files) >= 50, f"only {len(files)} plugin markdown files found: wrong root?"
    assert mentions >= 20, f"only {mentions} mentions: the scan saw nothing to check"
    assert not bad, "\n".join(bad)


_GITHUB_MAIN = re.compile(r"https://github\.com/Hellblazer/nexus/blob/main/([\w./-]+)")


def test_every_github_main_link_in_plugin_text_names_a_file_in_the_tree() -> None:
    """A link to the repo's main branch stands in for a repo-only path (nexus-4deer): a renamed or moved target would
    be a silent 404 for an agent in another project, so each target must exist in this checkout."""
    roots = (*PLUGIN_MD_DIRS, SHARED_DOCS_DIR)
    targets: set[tuple[str, str]] = set()
    for path in sorted(p for d in roots if d.is_dir() for p in d.rglob("*.md")):
        for m in _GITHUB_MAIN.finditer(path.read_text(encoding="utf-8")):
            targets.add((m.group(1).rstrip("."), str(path.relative_to(REPO_ROOT))))
    print(f"{len(targets)} GitHub main link targets in plugin text")
    assert len(targets) >= 5, f"only {len(targets)} links: the pattern saw nothing"
    missing = sorted(f"{src}: {t}" for t, src in targets if not (REPO_ROOT / t).is_file())
    assert not missing, "\n".join(missing)
