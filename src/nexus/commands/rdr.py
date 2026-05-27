# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx rdr`` — RDR authoring helpers.

Exposes:
  - ``lint``    : scan RDR markdown files for frontmatter parse hazards
  - ``preamble``: 9 lifecycle subcommands (RDR-130 P1.2)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import click
import yaml


# ---------------------------------------------------------------------------
# lint helpers (unchanged)
# ---------------------------------------------------------------------------

# Matches a flow-sequence opener followed (eventually) by an unquoted
# ``#`` before the closing ``]``. ``[^\]"']*?`` lets us span multiple
# lines (PyYAML's multi-line flow sequences parse silently into empty
# lists when ``#`` introduces comments mid-sequence — a true false
# negative for the single-line regex). The ``"'`` exclusion keeps quoted
# strings from being mis-flagged as the hazard.
_HASH_REF_IN_FLOW_SEQ = re.compile(r":\s*\[[^\]\"']*?#", re.DOTALL)


def _frontmatter_block(text: str) -> str | None:
    """Return the frontmatter block (without delimiters) or None."""
    if not text.startswith("---"):
        return None
    idx = text.find("\n---", 3)
    if idx == -1:
        return None
    return text[3:idx]


def _lint_one(path: Path) -> list[str]:
    """Return a list of human-readable findings for *path* (empty if clean)."""
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: read failed ({type(exc).__name__}: {exc})"]

    fm = _frontmatter_block(text)
    if fm is None:
        return findings

    # _frontmatter_block returns text starting at index 3 (right after the
    # opening ``---``), which is typically the trailing ``\n`` of that line.
    # Strip the leading newline so the first content line maps cleanly to
    # file line 2 (file line 1 is the opening ``---``).
    fm_body = fm.lstrip("\n")

    for m in _HASH_REF_IN_FLOW_SEQ.finditer(fm_body):
        # If the opener line is itself a YAML comment (``# note: [#381]``),
        # the ``: [`` is inside a comment and the whole thing is benign.
        # Find the start of the line containing the match opener and
        # check the first non-whitespace char.
        line_start = fm_body.rfind("\n", 0, m.start()) + 1
        if fm_body[line_start:m.start()].lstrip().startswith("#"):
            continue
        # Line number within fm_body. +2 for the opening ``---`` line.
        line_no = fm_body.count("\n", 0, m.start()) + 2
        snippet = fm_body[m.start():m.end()].replace("\n", " ").strip()
        findings.append(
            f"{path}:{line_no}: unquoted #-ref in YAML flow sequence "
            f"({snippet!r}); quote the refs: "
            f'prs: ["#381", "#382"]'
        )

    try:
        yaml.safe_load(fm)
    except yaml.YAMLError as exc:
        findings.append(f"{path}: frontmatter YAML parse error: {exc}")

    return findings


# ---------------------------------------------------------------------------
# preamble shared helpers (RDR-130 P1.2)
# ---------------------------------------------------------------------------

_PREAMBLE_EXCLUDED: frozenset[str] = frozenset({
    "readme.md", "template.md", "index.md", "overview.md",
    "workflow.md", "templates.md",
})


def _preamble_resolve_repo() -> tuple[str, str]:
    """Return (repo_root, repo_name) by probing git; fall back to cwd."""
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        repo_name = Path(repo_root).name
    except Exception:
        repo_root = str(Path.cwd())
        repo_name = Path(repo_root).name
    return repo_root, repo_name


def _preamble_rdr_dir(repo_root: str) -> str:
    """Resolve RDR directory from .nexus.yml or return 'docs/rdr'."""
    rdr_dir = "docs/rdr"
    nexus_yml = Path(repo_root) / ".nexus.yml"
    if nexus_yml.exists():
        content = nexus_yml.read_text()
        try:
            d = yaml.safe_load(content) or {}
            paths = (d.get("indexing") or {}).get("rdr_paths", ["docs/rdr"])
            rdr_dir = paths[0] if paths else "docs/rdr"
        except Exception:
            m_yml = (
                re.search(r"rdr_paths[^\[]*\[([^\]]+)\]", content)
                or re.search(r"rdr_paths:\s*\n\s+-\s*(.+)", content)
            )
            if m_yml:
                v = m_yml.group(1)
                parts = re.findall(r"[a-z][a-z0-9/_-]+", v)
                rdr_dir = parts[0] if parts else "docs/rdr"
    return rdr_dir


def _preamble_parse_frontmatter(filepath: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter from *filepath*; return (meta, full_text)."""
    text = filepath.read_text(errors="replace")
    meta: dict = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            block = parts[1]
            try:
                meta = yaml.safe_load(block) or {}
            except Exception:
                for line in block.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip().lower()] = v.strip()
    else:
        m = re.search(
            r"^## Metadata\s*\n(.*?)(?=^##|\Z)", text, re.MULTILINE | re.DOTALL
        )
        if m:
            for line in m.group(1).splitlines():
                kv = re.match(r"-?\s*\*\*(\w[\w\s]*?)\*\*:\s*(.+)", line.strip())
                if kv:
                    meta[kv.group(1).strip().lower()] = kv.group(2).strip()
    if "title" not in meta and "name" not in meta:
        h1 = re.search(r"^#\s+(.+)", text, re.MULTILINE)
        if h1:
            meta["title"] = h1.group(1).strip()
    return meta, text


def _preamble_find_rdr_file(rdr_path: Path, id_str: str) -> Path | None:
    """Find an RDR .md by numeric ID; return None if not found."""
    m = re.search(r"\d+", id_str)
    if not m:
        return None
    num_int = int(m.group(0))
    for f in sorted(rdr_path.glob("*.md")):
        nums = re.findall(r"\d+", f.stem)
        if nums and int(nums[0]) == num_int:
            return f
    return None


def _preamble_get_all_rdrs(rdr_path: Path) -> list[dict]:
    """Return a list of RDR dicts from .md files in *rdr_path*."""
    rdrs: list[dict] = []
    for f in sorted(rdr_path.glob("*.md")):
        if f.name.lower() in _PREAMBLE_EXCLUDED:
            continue
        fm, text = _preamble_parse_frontmatter(f)
        rtype = fm.get("type", "?")
        doc_status = fm.get("status", "?")
        if doc_status == "?" and rtype == "?":
            continue
        nums = re.findall(r"\d+", f.stem)
        rdrs.append({
            "id": nums[0] if nums else f.stem,
            "file": f.name,
            "path": f,
            "text": text,
            "title": fm.get("title", fm.get("name", f.stem)),
            "status": doc_status,
            "rtype": rtype,
            "priority": fm.get("priority", "?"),
        })
    return rdrs


def _preamble_parse_t2_field(content: str, field: str) -> str | None:
    """Extract a field value from T2 entry content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return val
    return None


def _preamble_get_rdrs_from_t2(repo_name: str, rdr_dir: str) -> list[dict]:
    """Read RDR list from T2; return [] if T2 is unavailable or empty."""
    rdrs: list[dict] = []
    try:
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database
        with T2Database(default_db_path()) as db:  # epsilon-allow: short-lived read-only preamble CLI
            entries = db.get_all(project=f"{repo_name}_rdr")
            for entry in entries:
                title = entry.get("title", "")
                if not re.match(r"^\d+$", title):
                    continue
                content = entry.get("content", "")
                rdrs.append({
                    "id": title,
                    "title": _preamble_parse_t2_field(content, "title") or title,
                    "status": _preamble_parse_t2_field(content, "status") or "?",
                    "rtype": _preamble_parse_t2_field(content, "type") or "?",
                    "priority": _preamble_parse_t2_field(content, "priority") or "?",
                    "file_path": (
                        _preamble_parse_t2_field(content, "file_path")
                        or f"{rdr_dir}/{title}-*.md"
                    ),
                })
    except Exception:
        pass
    return rdrs


# ---------------------------------------------------------------------------
# rdr group + lint command
# ---------------------------------------------------------------------------

@click.group()
def rdr() -> None:
    """RDR authoring helpers."""


@rdr.command("lint")
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Scan this directory recursively for *.md (default: docs/rdr/ if it exists).",
)
def lint(paths: tuple[Path, ...], root: Path | None) -> None:
    """Lint RDR frontmatter for parse hazards.

    Checks each *.md for frontmatter that would fail downstream YAML
    parsing — primarily the ``prs: [#NNN]`` flow-sequence hazard
    (nexus-u7ek). Exits non-zero when any finding is reported.
    """
    targets: list[Path] = []
    if paths:
        for p in paths:
            if p.is_dir():
                targets.extend(sorted(p.rglob("*.md")))
            else:
                targets.append(p)
    else:
        scan_root = root or Path("docs/rdr")
        if not scan_root.exists():
            click.echo(
                f"no paths given and {scan_root} not found; nothing to lint",
                err=True,
            )
            sys.exit(2)
        targets = sorted(scan_root.rglob("*.md"))

    all_findings: list[str] = []
    files_with_findings = 0
    for path in targets:
        per_file = _lint_one(path)
        if per_file:
            files_with_findings += 1
            all_findings.extend(per_file)

    if all_findings:
        for f in all_findings:
            click.echo(f, err=True)
        click.echo(
            f"\n{len(all_findings)} finding(s) in {files_with_findings} of "
            f"{len(targets)} file(s)",
            err=True,
        )
        sys.exit(1)

    click.echo(f"clean: {len(targets)} file(s) scanned")


# ---------------------------------------------------------------------------
# preamble subgroup (RDR-130 P1.2)
# ---------------------------------------------------------------------------

@rdr.group("preamble")
def preamble() -> None:
    """RDR lifecycle preamble subcommands (nx rdr preamble <name>)."""


# ---------------------------------------------------------------------------
# preamble rdr-list
# ---------------------------------------------------------------------------

@preamble.command("rdr-list")
@click.argument("args", nargs=-1)
def preamble_rdr_list(args: tuple[str, ...]) -> None:
    """List all RDRs (T2 primary, file fallback)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    # Primary: read from T2
    rdrs = _preamble_get_rdrs_from_t2(repo_name, rdr_dir)
    source = "T2"

    # Fallback: read from files if T2 is empty
    if not rdrs:
        rdrs = _preamble_get_all_rdrs(rdr_path)
        source = "files"

    print(f"### RDRs ({len(rdrs)} found, source: {source})")
    print()
    if rdrs:
        print("| ID | Title | Status | Type | Priority |")
        print("|----|-------|--------|------|----------|")
        for r in rdrs:
            print(f"| {r['id']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
    else:
        print(f"No RDRs found in `{rdr_dir}`")


# ---------------------------------------------------------------------------
# preamble rdr-create
# ---------------------------------------------------------------------------

@preamble.command("rdr-create")
@click.argument("args", nargs=-1)
def preamble_rdr_create(args: tuple[str, ...]) -> None:
    """Print context for creating a new RDR."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> RDR directory `{rdr_dir}` does not exist — bootstrap required.")
        print()
        print("**Next ID:** `RDR-001`")
        print("**ID style detected:** `RDR-NNN-kebab-title.md` (default — no existing files)")
        print()
        print("### Existing RDRs (0 found)")
        print()
        print("None — this will be the first RDR.")
    else:
        rdrs = _preamble_get_all_rdrs(rdr_path)

        # Detect ID style from existing files (case-insensitive for RDR- prefix)
        rdr_prefix_style = False
        numeric_style = False
        for r in rdrs:
            if re.match(r"^[Rr][Dd][Rr]-\d+", r["file"]):
                rdr_prefix_style = True
                break
            elif re.match(r"^\d+", r["file"]):
                numeric_style = True

        if rdr_prefix_style:
            id_style = "RDR-NNN-kebab-title.md"
        elif numeric_style:
            id_style = "NNN-kebab-title.md"
        else:
            id_style = "RDR-NNN-kebab-title.md"

        # Compute next sequential ID
        max_num = 0
        for r in rdrs:
            nums = re.findall(r"\d+", r["file"])
            if nums:
                max_num = max(max_num, int(nums[0]))
        next_num = max_num + 1

        if rdr_prefix_style:
            next_id = f"RDR-{next_num:03d}"
        else:
            next_id = f"{next_num:03d}"

        print(f"**Next ID:** `{next_id}`")
        print(f"**ID style detected:** `{id_style}`")
        print()
        print(f"### Existing RDRs ({len(rdrs)} found)")
        print()
        if rdrs:
            print("| File | Title | Status |")
            print("|------|-------|--------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} |")
        else:
            print("None — this will be the first RDR.")

    print()

    # Active beads (for Related Issues field)
    print("### Active Beads (for Related Issues field)")
    try:
        result = subprocess.run(
            ["bd", "list", "--status=in_progress", "--limit=5"],
            capture_output=True, text=True, timeout=10,
        )
        bd_out = (result.stdout or "").strip()
        print(bd_out if bd_out else "No in-progress beads")
    except Exception as exc:
        print(f"Beads not available: {exc}")
    print()


# ---------------------------------------------------------------------------
# preamble rdr-show
# ---------------------------------------------------------------------------

def _preamble_get_excerpt(text: str) -> str:
    """Strip frontmatter and return a 250-char content excerpt."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) >= 3 else text
    else:
        m = re.search(r"^## Metadata\s*\n.*?(?=^##)", text, re.MULTILINE | re.DOTALL)
        if m:
            text = text[m.end():]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return " ".join(lines)[:250]


@preamble.command("rdr-show")
@click.argument("args", nargs=-1)
def preamble_rdr_show(args: tuple[str, ...]) -> None:
    """Show RDR list or details for a specific RDR."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    if args_str:
        # Show specific RDR
        rdr_file = _preamble_find_rdr_file(rdr_path, args_str)
        if rdr_file:
            fm, text = _preamble_parse_frontmatter(rdr_file)
            print(f"### RDR: {rdr_file.name}")
            print()

            # Metadata table
            print("#### Metadata")
            print()
            print("| Field | Value |")
            print("|-------|-------|")
            for key in ("status", "type", "priority", "title", "author", "date",
                        "supersedes", "superseded-by"):
                val = fm.get(key)
                if val:
                    print(f"| {key.title()} | {val} |")
            print()

            # Full content
            print("#### Content")
            print()
            print(text)
            print()

            # T2 metadata (printed as instruction; no direct T2 read needed here)
            rdr_num = re.search(r"\d+", rdr_file.stem)
            t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem
            print("### T2 Metadata")
            try:
                t2_result = subprocess.run(
                    ["nx", "memory", "get", "--project", f"{repo_name}_rdr",
                     "--title", t2_key],
                    capture_output=True, text=True, timeout=10,
                )
                t2_out = (t2_result.stdout or "").strip()
                print(t2_out if t2_out else f"No T2 record for RDR {t2_key}")
            except Exception as exc:
                print(f"T2 not available: {exc}")
            print()

            # T2 research findings
            print("### T2 Research Findings")
            try:
                list_result = subprocess.run(
                    ["nx", "memory", "list", "--project", f"{repo_name}_rdr"],
                    capture_output=True, text=True, timeout=10,
                )
                list_out = (list_result.stdout or "").strip()
                research_lines = [
                    ln for ln in list_out.splitlines()
                    if re.match(rf"^{t2_key}-research", ln)
                ]
                print("\n".join(research_lines) if research_lines
                      else "No research findings recorded")
            except Exception as exc:
                print(f"T2 not available: {exc}")
            print()

            # Linked beads
            print("### Linked Beads")
            try:
                bd_result = subprocess.run(
                    ["bd", "list", "--status=open", "--limit=20"],
                    capture_output=True, text=True, timeout=10,
                )
                bd_out = (bd_result.stdout or "").strip()
                matching = [
                    ln for ln in bd_out.splitlines()
                    if re.search(rf"rdr.*{t2_key}|{t2_key}.*rdr", ln, re.IGNORECASE)
                ]
                print("\n".join(matching) if matching
                      else "No beads linked (check epic_bead in T2)")
            except Exception as exc:
                print(f"Beads not available: {exc}")
        else:
            print(f"> RDR not found for: `{args_str}`")
            print()
            print("Available RDRs:")
            rdrs = _preamble_get_all_rdrs(rdr_path)
            if rdrs:
                print()
                print("| File | Title | Status | Type | Priority |")
                print("|------|-------|--------|------|----------|")
                for r in rdrs:
                    print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
    else:
        # No ID — show list (most recently modified first)
        all_md = [f for f in rdr_path.glob("*.md")
                  if f.name.lower() not in _PREAMBLE_EXCLUDED]
        all_md_sorted = sorted(all_md, key=lambda f: f.stat().st_mtime, reverse=True)

        rdrs = []
        for f in all_md_sorted:
            fm, text = _preamble_parse_frontmatter(f)
            rtype = fm.get("type", "?")
            doc_status = fm.get("status", "?")
            if doc_status == "?" and rtype == "?":
                continue
            rdrs.append({
                "file": f.name,
                "path": f,
                "text": text,
                "title": fm.get("title", fm.get("name", f.stem)),
                "status": doc_status,
                "rtype": rtype,
                "priority": fm.get("priority", "?"),
            })

        print(f"### RDR Files ({len(rdrs)} found, most recently modified first)")
        print()
        if rdrs:
            print("| File | Title | Status | Type | Priority |")
            print("|------|-------|--------|------|----------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
            print()
            print("### Content Index (for keyword and topic filtering)")
            print()
            for r in rdrs:
                excerpt = _preamble_get_excerpt(r["text"])
                print(f"**{r['file']}**: {excerpt}")
        else:
            print(f"No RDR files found in `{rdr_dir}`")


# ---------------------------------------------------------------------------
# preamble rdr-gate
# ---------------------------------------------------------------------------

@preamble.command("rdr-gate")
@click.argument("args", nargs=-1)
def preamble_rdr_gate(args: tuple[str, ...]) -> None:
    """Print RDR gate context (gap check + section structure)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    id_match = re.search(r"\d+", args_str)

    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-gate <id>`")
        print()
        rdrs = _preamble_get_all_rdrs(rdr_path)
        print("### Available RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

    print(f"### RDR File: {rdr_file.name}")
    print(f"**Title:** {title}  **Status:** {fm.get('status', '?')}  **Type:** {fm.get('type', '?')}")
    print()

    def _strip_code_blocks(src: str) -> str:
        return re.sub(r"```.*?```", "", src, flags=re.DOTALL)

    # Gap-structure pre-check for post-65 RDRs
    _skip_gaps = "--skip-gaps" in args_str
    _problem_idx = text.find("## Problem Statement")
    if _problem_idx == -1:
        _problem_idx = text.find("## Problem")
    _problem_section = ""
    if _problem_idx != -1:
        _rest = text[_problem_idx:]
        _nxt = re.search(r"\n## ", _rest[1:])
        _problem_section = _rest[:_nxt.start() + 1] if _nxt else _rest
    _gap_headings = re.findall(
        r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", _problem_section, re.MULTILINE
    )
    try:
        _rdr_id_int = int(t2_key)
    except ValueError:
        _rdr_id_int = -1

    if _rdr_id_int >= 65 and len(_gap_headings) == 0 and not _skip_gaps:
        print(
            f"> **BLOCKED** (Layer 1 — gap structure): RDR-{t2_key} has no "
            f"`#### Gap N: <title>` headings in `## Problem Statement` or `## Problem`."
        )
        print(r"> Expected format: `#### Gap 1: <gap title>` (regex: `^#{3,5} Gap \d+:`).")
        print(">")
        print(
            "> The close skill enforces the same structure and will block closing. "
            "Add the headings now before accept, or re-run the gate "
            "with `--skip-gaps` to record an intentional override."
        )
        return
    elif _rdr_id_int >= 65 and len(_gap_headings) > 0:
        print(f"#### Gap structure: {len(_gap_headings)} gap heading(s) present")
        print()
        for _num, _qual, _title in _gap_headings:
            _qual_str = _qual.strip()
            _qual_disp = f" {_qual_str}" if _qual_str else ""
            print(f"- Gap {_num}{_qual_disp}: {_title.strip()}")
        print()
    elif _rdr_id_int < 65 and len(_gap_headings) == 0:
        print(
            f"> **Note**: RDR-{t2_key} predates the gap-structure convention (id < 65) — "
            "skipping the Layer 1 gap check."
        )
        print()

    clean = _strip_code_blocks(text)

    # Section headings
    headings = re.findall(r"^(#{1,3} .+)", clean, re.MULTILINE)
    print("#### Section Structure (for completeness check)")
    print()
    for h in headings:
        print(h)
    print()

    # Section summaries
    print("#### Section Summaries")
    print()
    sections = re.split(r"^(## .+)", clean, flags=re.MULTILINE)
    for i in range(1, len(sections) - 1, 2):
        heading = sections[i].strip()
        body = sections[i + 1]
        first_lines = [ln.strip() for ln in body.splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
        summary = first_lines[0][:120] if first_lines else "_empty_"
        print(f"**{heading}**: {summary}")
    print()

    # T2 metadata (instruction only)
    print("### T2 Metadata")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" "
        "to retrieve T2 metadata."
    )
    print()

    # T2 research findings (instruction only)
    print("### T2 Research Findings")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"\" "
        f"to list all entries, then filter for {t2_key}-research* titles."
    )
    print(
        f"If no research findings exist, run `nx rdr preamble rdr-research -- {t2_key}` "
        "to record findings before gating."
    )


# ---------------------------------------------------------------------------
# preamble rdr-accept
# ---------------------------------------------------------------------------

@preamble.command("rdr-accept")
@click.argument("args", nargs=-1)
def preamble_rdr_accept(args: tuple[str, ...]) -> None:
    """Print RDR accept context and planning handoff."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    id_match = re.search(r"\d+", args_str)

    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-accept <id>`")
        print()
        rdrs = _preamble_get_all_rdrs(rdr_path)
        draft_rdrs = [r for r in rdrs if r["status"].lower() == "draft"]
        print("### Draft RDRs (eligible for acceptance)")
        print()
        if draft_rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in draft_rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print("No Draft RDRs found. Only Draft RDRs can be accepted.")
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    current_status = fm.get("status", "?")
    rdr_type = fm.get("type", "?")
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

    print(f"### RDR: {rdr_file.name}")
    print(
        f"**RDR ID:** {t2_key}  **Title:** {title}  "
        f"**Type:** {rdr_type}  **File Status:** {current_status}"
    )
    print()

    # Accepted status is allowed — agent handles idempotency
    if current_status.lower() not in ("draft", "accepted"):
        print(
            f"> **BLOCKED**: RDR status is `{current_status}`. "
            "Only Draft RDRs can be accepted."
        )
        return

    # T2 lookups — printed as instructions
    print("### T2 Lookups (call these before executing Action steps)")
    print()
    print(
        f"1. **T2 metadata**: Use **memory_get** tool: "
        f"project=\"{repo_name}_rdr\", title=\"{t2_key}\""
    )
    print(
        f"2. **T2 gate result**: Use **memory_get** tool: "
        f"project=\"{repo_name}_rdr\", title=\"{t2_key}-gate-latest\""
    )
    print(
        f"   If no gate record exists, run `nx rdr preamble rdr-gate -- {t2_key}` first."
    )
    print()
    print(f"**RDR file path:** `{rdr_file}`")
    print()

    # Step count auto-detection
    plan_headers = [
        r"^## Implementation Plan",
        r"^## Approach",
        r"^## Plan",
        r"^## Design",
        r"^## Steps",
        r"^## Execution",
    ]
    plan_section = None
    for hdr in plan_headers:
        m = re.search(hdr + r"\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        if m:
            plan_section = m.group(1)
            break

    step_count = 0
    has_plan = plan_section is not None
    if has_plan:
        step_count = len(re.findall(
            r"^### (?:Phase|Step|Stage|Part)\s", plan_section, re.MULTILINE
        ))
        if step_count == 0:
            step_count = len(re.findall(r"^### \d", plan_section, re.MULTILINE))

    print("### Planning Handoff")
    print(f"**Step count detected:** {step_count}")
    print(f"**Has plan section:** {'yes' if has_plan else 'no'}")
    if step_count >= 2:
        print("**Recommendation:** Invoke strategic planner (multi-step RDR)")
        print("**Default:** yes")
    else:
        print("**Recommendation:** Invoke strategic planner")
        print("**Default:** yes")
    print()


# ---------------------------------------------------------------------------
# preamble rdr-close
# ---------------------------------------------------------------------------

@preamble.command("rdr-close")
@click.argument("args", nargs=-1)
def preamble_rdr_close(args: tuple[str, ...]) -> None:
    """Print RDR close context (gap check + T2 metadata instructions)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    # Strip flags from args before extracting ID
    reason_match = re.search(r"--reason\s+(\S+)", args_str)
    close_reason = reason_match.group(1) if reason_match else None
    force = bool(re.search(r"--force(?!-)", args_str))
    pointers_match = (
        re.search(r"--pointers\s+'([^']+)'", args_str)
        or re.search(r'--pointers\s+"([^"]+)"', args_str)
        or re.search(r"--pointers\s+(\S+)", args_str)
    )
    pointers_arg = pointers_match.group(1) if pointers_match else None
    force_implemented_match = (
        re.search(r"--force-implemented\s+'([^']*)'", args_str)
        or re.search(r'--force-implemented\s+"([^"]*)"', args_str)
        or re.search(r"--force-implemented\s+(\S+)", args_str)
    )
    force_implemented_reason = (
        force_implemented_match.group(1) if force_implemented_match else None
    )

    args_clean = re.sub(r"--reason\s+\S+", "", args_str)
    args_clean = re.sub(r"--force-implemented\s+'[^']*'", "", args_clean)
    args_clean = re.sub(r'--force-implemented\s+"[^"]*"', "", args_clean)
    args_clean = re.sub(r"--force-implemented\s+\S+", "", args_clean)
    args_clean = re.sub(r"--force(?!-)", "", args_clean)
    args_clean = re.sub(r"--pointers\s+'[^']+'", "", args_clean)
    args_clean = re.sub(r'--pointers\s+"[^"]+"', "", args_clean)
    args_clean = re.sub(r"--pointers\s+\S+", "", args_clean).strip()

    id_match = re.search(r"\d+", args_clean)

    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-close <id> [--reason implemented|...]`")
        print()
        rdrs = _preamble_get_all_rdrs(rdr_path)
        print("### Open/Draft RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    current_status = fm.get("status", "?")
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

    print(f"### RDR: {rdr_file.name}")
    print(f"**Title:** {title}  **Current Status:** {current_status}")
    if close_reason:
        print(f"**Close Reason:** {close_reason}")
    if force_implemented_reason:
        print(f"**Force Implemented (audit):** {force_implemented_reason}")
    print()

    # Hard-block: refuse to close unless status is accepted or final
    if current_status.lower() not in ("accepted", "final"):
        if force:
            print(
                f"> **Override**: RDR status is `{current_status}` (not accepted/final). "
                "Proceeding with `--force`."
            )
            print()
        else:
            print(
                f"> **BLOCKED**: RDR status is `{current_status}`. "
                "Close requires status `accepted` or `final`."
            )
            print("> Run `nx rdr preamble rdr-gate` to validate, or use `--force` to override.")
            print()
            return

    # Gap-check for --reason implemented
    if (close_reason or "").lower() == "implemented":
        def _extract_section(doc: str, *headings: str) -> str:
            for heading in headings:
                idx = doc.find(heading)
                if idx != -1:
                    rest = doc[idx + len(heading):]
                    nxt = re.search(r"\n## ", rest)
                    return rest[:nxt.start()] if nxt else rest
            return ""

        def _parse_pointers(s: str) -> dict[str, str]:
            out: dict[str, str] = {}
            for tok in s.split(","):
                tok = tok.strip()
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    out[k.strip()] = v.strip()
            return out

        try:
            rdr_id_int = int(t2_key)
        except ValueError:
            rdr_id_int = -1

        problem_stmt = _extract_section(text, "## Problem Statement", "## Problem")
        gap_matches = re.findall(
            r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", problem_stmt, re.MULTILINE
        )
        gap_count = len(gap_matches)

        if rdr_id_int < 65 and gap_count == 0:
            print("> **WARN**: This RDR predates structured gaps; no action required.")
            print()
        elif rdr_id_int >= 65 and gap_count == 0:
            print(
                f"> **ERROR**: RDR-{t2_key} has no `#### Gap N: <title>` headings "
                "in `## Problem Statement` or `## Problem`."
            )
            print(r"> Expected format: `#### Gap 1: <gap title>` (regex: `^#{3,5} Gap \d+:`)")
            print()
            return
        elif gap_count > 0 and not pointers_arg:
            print("### Problem Statement Gaps")
            print()
            for num, qual, gap_title in gap_matches:
                qual_str = qual.strip()
                qual_disp = f" {qual_str}" if qual_str else ""
                print(f"- Gap{num}{qual_disp}: {gap_title.strip()}")
            print()
            print("**Re-invoke with per-gap closure pointers:**")
            print()
            example = ",".join(
                f"Gap{num}=path/to/file.py:LINE" for num, _q, _t in gap_matches
            )
            print(
                f"nx rdr preamble rdr-close -- {t2_key} --reason implemented "
                f"--pointers '{example}'"
            )
            print()
            return
        else:
            # PASS 2: validate pointers
            pointers = _parse_pointers(pointers_arg or "")
            failures = []
            for num, _qual, _title in gap_matches:
                gap_key = f"Gap{num}"
                if gap_key not in pointers:
                    failures.append(f"{gap_key}: no pointer supplied")
                    continue
                ptr = pointers[gap_key]
                file_part, sep, line_part = ptr.partition(":")
                if not sep:
                    failures.append(
                        f"{gap_key}: pointer '{ptr}' missing ':LINE' — expected file:line shape"
                    )
                    continue
                if not re.match(r"^\d+", line_part):
                    failures.append(
                        f"{gap_key}: pointer '{ptr}' has no line number after ':'"
                    )
                    continue
                if not (Path(repo_root) / file_part).exists():
                    failures.append(f"{gap_key}: file '{file_part}' does not exist in repo")
            if failures:
                print("> **ERROR**: Problem Statement pointer validation failed:")
                for f in failures:
                    print(f">   - {f}")
                print()
                return
            # Passed
            print("### PROBLEM STATEMENT REPLAY: validation passed")
            print()
            for gap_key, ptr in sorted(pointers.items()):
                print(f"- {gap_key} → {ptr}")
            print()

    # T2 metadata
    print("### T2 Metadata (current status)")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" "
        "to retrieve T2 metadata."
    )
    print()

    # Bead status advisory
    print("### Bead Status Advisory")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" "
        "to check for `epic_bead` field."
    )
    print()

    # Active beads
    print("### Active Beads")
    try:
        bd_result = subprocess.run(
            ["bd", "list", "--status=open,in_progress", "--limit=20"],
            capture_output=True, text=True, timeout=10,
        )
        bd_out = (bd_result.stdout or "").strip()
        if bd_out and bd_out != "No issues found.":
            print(bd_out)
        else:
            print("No open or in-progress beads.")
    except Exception as exc:
        print(f"Beads not available: {exc}")


# ---------------------------------------------------------------------------
# preamble rdr-research
# ---------------------------------------------------------------------------

@preamble.command("rdr-research")
@click.argument("args", nargs=-1)
def preamble_rdr_research(args: tuple[str, ...]) -> None:
    """Print RDR research context (file Research Findings + T2 entries)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    # Extract numeric ID from args (skip subcommand words like "add", "status")
    id_match = re.search(r"\d+", args_str)

    if id_match:
        rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
        if rdr_file:
            fm, text = _preamble_parse_frontmatter(rdr_file)
            title = fm.get("title", fm.get("name", rdr_file.stem))
            rdr_num = re.search(r"\d+", rdr_file.stem)
            # Strip leading zeros so "001" -> "1" for display
            t2_key = str(int(rdr_num.group(0))) if rdr_num else rdr_file.stem

            print(f"### RDR {t2_key}: {title}")
            print(f"**File:** `{rdr_file.name}`")
            print()

            rf_match = re.search(
                r"^## Research Findings\s*\n(.*?)(?=^## |\Z)",
                text, re.MULTILINE | re.DOTALL,
            )
            print("#### Research Findings (from file)")
            print()
            if rf_match:
                section = rf_match.group(1).strip()
                print(
                    section if section
                    else "_No content in Research Findings section yet._"
                )
            else:
                print("_No `## Research Findings` section found in this RDR._")
            print()

            # T2 research findings
            print("### Existing Research Findings (T2)")
            try:
                list_result = subprocess.run(
                    ["nx", "memory", "list", "--project", f"{repo_name}_rdr"],
                    capture_output=True, text=True, timeout=10,
                )
                list_out = (list_result.stdout or "").strip()
                research_lines = [
                    ln for ln in list_out.splitlines()
                    if re.match(rf"^{t2_key}-research", ln)
                ]
                print(
                    "\n".join(research_lines) if research_lines
                    else "No research findings recorded yet"
                )
            except Exception as exc:
                print(f"T2 not available: {exc}")
        else:
            print(f"> RDR not found for ID: `{id_match.group(0)}`")
            print()
            rdrs = _preamble_get_all_rdrs(rdr_path)
            print("### Available RDRs")
            print()
            if rdrs:
                print("| File | Title | Status | Type |")
                print("|------|-------|--------|------|")
                for r in rdrs:
                    print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
            else:
                print(f"No RDRs found in `{rdr_dir}`")
    else:
        # No ID — show available RDRs
        rdrs = _preamble_get_all_rdrs(rdr_path)
        print("### Available RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
        print()
        print(
            "> **Usage**: `nx rdr preamble rdr-research -- <id>` or "
            "`nx rdr preamble rdr-research -- add <id>`"
        )


# ---------------------------------------------------------------------------
# preamble rdr-audit
# ---------------------------------------------------------------------------

@preamble.command("rdr-audit")
@click.argument("args", nargs=-1)
def preamble_rdr_audit(args: tuple[str, ...]) -> None:
    """Print RDR audit dispatch context."""
    args_str = " ".join(args).strip()

    # Derive current project name: git remote -> git root -> cwd
    def _derive_project_name() -> str:
        try:
            url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            if url:
                name = url.rsplit("/", 1)[-1]
                if name.endswith(".git"):
                    name = name[:-4]
                if name:
                    return name
        except Exception:
            pass
        try:
            root = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            if root:
                return Path(root).name
        except Exception:
            pass
        return Path.cwd().name

    current_project = _derive_project_name()

    _READONLY_SUBCOMMANDS = {"list", "status", "history"}
    _PRINTONLY_SUBCOMMANDS = {"schedule", "unschedule"}
    _SUBCOMMANDS = _READONLY_SUBCOMMANDS | _PRINTONLY_SUBCOMMANDS

    first_token = args_str.split()[0] if args_str else ""
    if first_token in _SUBCOMMANDS:
        subcommand = first_token
        target = args_str[len(first_token):].strip() or (
            current_project if subcommand != "list" else ""
        )
        safety_class = (
            "read-only" if subcommand in _READONLY_SUBCOMMANDS else "print-only"
        )
        print(f"**Mode:** management subcommand `{subcommand}` ({safety_class})")
        if target:
            print(f"**Target project:** `{target}`")
        else:
            print("**Scope:** all scheduled audits on this machine")
        print()
        if safety_class == "read-only":
            print(
                f"> `{subcommand}` is read-only — no OS state mutation, "
                "no T2 state mutation."
            )
        else:
            print(
                f"> `{subcommand}` is print-only — prints install/uninstall instructions "
                "for user review."
            )
        print()
    else:
        target = first_token or current_project
        print("**Mode:** audit dispatch (default)")
        print(
            f"**Target project:** `{target}`"
            + (" (derived from current repo)" if not first_token else "")
        )
        print()

        home = Path.home()
        import os
        roots_env = os.environ.get("NEXUS_PROJECT_ROOTS", "").strip()
        if roots_env:
            roots = [Path(os.path.expanduser(r)) for r in roots_env.split(":") if r]
            roots_source = "NEXUS_PROJECT_ROOTS"
        else:
            roots = [
                home / "git",
                home / "src",
                home / "projects",
                home / "code",
                home / "work",
                home / "dev",
                home / "Documents" / "git",
            ]
            roots_source = "default candidates (set NEXUS_PROJECT_ROOTS to override)"

        candidate_paths = [r / target for r in roots if r.is_dir()]
        found_path = next(
            (p for p in candidate_paths if p.exists() and p.is_dir()), None
        )
        if found_path:
            print(f"**Worktree found:** `{found_path}`")
            postmortem_dir = found_path / "docs" / "rdr" / "post-mortem"
            if postmortem_dir.exists():
                count = len(list(postmortem_dir.glob("*.md")))
                print(f"**Post-mortems available:** {count} files in `{postmortem_dir}`")
            else:
                print(
                    f"> No `docs/rdr/post-mortem/` directory found at `{found_path}`."
                )
        else:
            probed = (
                ", ".join(str(r) for r in roots if r.is_dir())
                or "(no existing roots)"
            )
            print(
                f"> No local worktree found for `{target}`. "
                f"Probed roots ({roots_source}): {probed}."
            )
            print("> Set `NEXUS_PROJECT_ROOTS` to the directory(ies) for project worktrees.")

        claude_projects = home / ".claude" / "projects"
        if claude_projects.exists():
            match_candidates = list(claude_projects.glob(f"*{target}*"))
            if match_candidates:
                print(
                    f"**Session transcripts available:** {len(match_candidates)} "
                    f"matching directory entries in `~/.claude/projects/`"
                )
            else:
                print(
                    f"> No session transcripts found for `{target}` "
                    "under `~/.claude/projects/`."
                )

    print()


# ---------------------------------------------------------------------------
# preamble phase-review-gate
# ---------------------------------------------------------------------------

def _prg_extract_approach_section(text: str) -> str:
    """Extract content under §Approach (handles ### and ## variants)."""
    for pat in [
        r"\n### Approach[^\n]*\n",
        r"\n## Approach[^\n]*\n",
        r"\n#### Approach[^\n]*\n",
    ]:
        m = re.search(pat, text)
        if m:
            start = m.end()
            heading_depth = len(
                re.match(r"(#+)", m.group(0).strip()).group(1)
            )
            end_pat = r"\n#{1," + str(heading_depth) + r"} "
            nxt = re.search(end_pat, text[start:])
            return text[start: start + nxt.start()] if nxt else text[start:]
    return ""


def _prg_parse_approach_items(
    approach_text: str,
) -> list[tuple[int, str, str]]:
    """Parse numbered bold items from §Approach text.

    Returns list of (item_num, label, summary).
    """
    items: list[tuple[int, str, str]] = []
    lines = approach_text.splitlines()
    current_num: int | None = None
    current_label = ""
    current_lines: list[str] = []

    for line in lines:
        m = re.match(r"^(\d+)\.\s+\*\*([^*]+)\*\*[:\s]*(.*)", line)
        if m:
            if current_num is not None:
                items.append(
                    (current_num, current_label, " ".join(current_lines).strip())
                )
            current_num = int(m.group(1))
            current_label = m.group(2).strip()
            current_lines = [m.group(3).strip()] if m.group(3).strip() else []
        elif current_num is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith("-"):
                current_lines.append(stripped)

    if current_num is not None:
        items.append(
            (current_num, current_label, " ".join(current_lines).strip())
        )
    return items


def _prg_parse_evidence(evidence_str: str) -> dict[int, str]:
    """Parse 'Item1=val1,Item2=val2,...' -> {1: 'val1', 2: 'val2'}."""
    out: dict[int, str] = {}
    for tok in evidence_str.split(","):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        num_m = re.search(r"(\d+)", k.strip())
        if num_m:
            out[int(num_m.group(1))] = v.strip()
    return out


@preamble.command("phase-review-gate")
@click.argument("args", nargs=-1)
def preamble_phase_review_gate(args: tuple[str, ...]) -> None:
    """Cross-walk §Approach items against closing beads at a phase boundary."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    # Parse flags
    phase_match = re.search(r"--phase\s+(\S+)", args_str)
    phase_arg = phase_match.group(1) if phase_match else None

    evidence_match = (
        re.search(r"--evidence\s+'([^']+)'", args_str)
        or re.search(r'--evidence\s+"([^"]+)"', args_str)
        or re.search(r"--evidence\s+(\S+)", args_str)
    )
    evidence_arg = evidence_match.group(1) if evidence_match else None

    # Strip flags to find RDR ID
    args_clean = re.sub(r"--phase\s+\S+", "", args_str)
    args_clean = re.sub(r"--evidence\s+'[^']+'", "", args_clean)
    args_clean = re.sub(r'--evidence\s+"[^"]+"', "", args_clean)
    args_clean = re.sub(r"--evidence\s+\S+", "", args_clean).strip()

    id_match = re.search(r"\d+", args_clean)

    if not id_match:
        print(
            "> **Usage**: `nx rdr preamble phase-review-gate -- <id> "
            "--phase <N> [--evidence 'Item1=bead-id,...']`"
        )
        print()
        print("### What this gate does")
        print()
        print(
            "At each phase-review boundary, cross-walk the RDR §Approach sub-items "
            "against the closing beads."
        )
        print(
            "Pass 1 enumerates items; Pass 2 validates evidence."
        )
        print()
        print("**Pass 1** (no --evidence): list approach items for the phase.")
        print("**Pass 2** (with --evidence): validate every item has an evidence pointer.")
        print()
        print("Evidence format: `Item1=nexus-abc1,Item2=nexus-xyz2,Item3=none`")
        print(
            "Use `none` for items explicitly deferred or acknowledged as out-of-phase scope."
        )
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> **ERROR**: RDR not found for ID: `{id_match.group(0)}`")
        print(f"> Looked in: `{rdr_path}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    rdr_num_m = re.search(r"\d+", rdr_file.stem)
    rdr_id_label = rdr_num_m.group(0) if rdr_num_m else rdr_file.stem

    print(f"**Repo:** `{repo_name}`  **RDR:** `{rdr_file.name}`")
    print(f"**Title:** {title}")
    print(f"**Phase:** {phase_arg or '(not specified)'}")
    print()

    # Extract §Approach items
    approach_text = _prg_extract_approach_section(text)
    if not approach_text.strip():
        print("> **ERROR**: No `### Approach` section found in this RDR.")
        print("> Phase-review gate requires §Approach to cross-walk against closing beads.")
        return

    items = _prg_parse_approach_items(approach_text)
    if not items:
        print("> **ERROR**: §Approach section found but no numbered items parsed.")
        print("> Expected format: `N. **Label**: description`")
        return

    # === PASS 1: enumerate approach items ===
    if not evidence_arg:
        print(f"### §Approach Cross-Walk — Phase {phase_arg or '?'}")
        print()
        print(
            "Enumerate each numbered §Approach item below, then provide an evidence pointer "
            "for each item."
        )
        print()
        print("| # | Label | Evidence needed |")
        print("|---|-------|-----------------|")
        for num, label, _summary in items:
            print(f"| Item{num} | **{label}** | (provide bead-id or `none`) |")
        print()
        example_parts = ",".join(f"Item{num}=nexus-xxxx" for num, _, _ in items)
        print("**Re-invoke with evidence once all items are accounted for:**")
        print()
        print(
            f"nx rdr preamble phase-review-gate -- {rdr_id_label} "
            f"--phase {phase_arg or '1'} --evidence '{example_parts}'"
        )
        print()
        return

    # === PASS 2: validate evidence coverage ===
    evidence = _prg_parse_evidence(evidence_arg)
    failures: list[tuple[int, str, str]] = []
    covered: list[tuple[int, str, str]] = []

    for num, label, _summary in items:
        val = evidence.get(num, "").strip()
        if not val:
            failures.append((num, label, "no evidence pointer supplied"))
        else:
            covered.append((num, label, val))

    if failures:
        print(f"> **BLOCKED** — Phase {phase_arg or '?'} cross-walk incomplete.")
        print(
            f"> {len(failures)} of {len(items)} approach item(s) have no evidence pointer."
        )
        print()
        print("### Missing Evidence")
        print()
        for num, label, reason in failures:
            print(f"- **Item{num}** ({label}): {reason}")
        print()
        print("These items must be accounted for before closing this phase.")
        print()
        return

    # All items covered
    print(f"### APPROACH CROSS-WALK PASSED — Phase {phase_arg or '?'}")
    print()
    print(f"All {len(items)} §Approach items accounted for:")
    print()
    for num, label, val in covered:
        print(f"- Item{num} ({label}) → `{val}`")
    print()
    print("> The gate verifies every §Approach item has a named evidence pointer.")
    print("> Review each pointer manually before allowing the phase close to proceed.")
    print()

    # Write T1 scratch marker (best-effort)
    try:
        subprocess.run(
            [
                "nx", "scratch", "put",
                f"phase-review-gate PASSED: RDR-{rdr_id_label} Phase {phase_arg}",
                "--tags",
                f"phase-review-passed,rdr-{rdr_id_label},phase-{phase_arg}",
            ],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    # Write phase_review_sentinel (best-effort, RDR-121 P2 co-requirement)
    try:
        from nexus.phase_review_sentinel import write_sentinel
        write_sentinel(rdr_id_label, str(phase_arg or "1"))
    except Exception:
        pass
