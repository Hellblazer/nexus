---
description: Run finalization gate on an RDR — structural, assumption audit, and AI critique
---

# RDR Gate

!{
NEXUS_RDR_ARGS="${ARGUMENTS:-}" python3 << 'PYEOF'
import os, sys, re, subprocess
from pathlib import Path

args = os.environ.get('NEXUS_RDR_ARGS', '').strip()

# Repo root and name
try:
    repo_root = subprocess.check_output(
        ['git', 'rev-parse', '--show-toplevel'],
        stderr=subprocess.DEVNULL, text=True).strip()
    repo_name = os.path.basename(repo_root)
except Exception:
    repo_root = os.getcwd()
    repo_name = os.path.basename(repo_root)

# Resolve RDR directory from .nexus.yml (default: docs/rdr)
rdr_dir = 'docs/rdr'
nexus_yml = Path(repo_root) / '.nexus.yml'
if nexus_yml.exists():
    content = nexus_yml.read_text()
    try:
        import yaml
        d = yaml.safe_load(content) or {}
        paths = (d.get('indexing') or {}).get('rdr_paths', ['docs/rdr'])
        rdr_dir = paths[0] if paths else 'docs/rdr'
    except ImportError:
        m_yml = (re.search(r'rdr_paths[^\[]*\[([^\]]+)\]', content) or
                 re.search(r'rdr_paths:\s*\n\s+-\s*(.+)', content))
        if m_yml:
            v = m_yml.group(1)
            parts = re.findall(r'[a-z][a-z0-9/_-]+', v)
            rdr_dir = parts[0] if parts else 'docs/rdr'

rdr_path = Path(repo_root) / rdr_dir

print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
print()


def parse_frontmatter(filepath):
    text = filepath.read_text(errors='replace')
    meta = {}
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            block = parts[1]
            try:
                import yaml; meta = yaml.safe_load(block) or {}
            except Exception:
                for line in block.splitlines():
                    if ':' in line:
                        k, _, v = line.partition(':')
                        meta[k.strip().lower()] = v.strip()
    if 'title' not in meta and 'name' not in meta:
        h1 = re.search(r'^#\s+(.+)', text, re.MULTILINE)
        if h1:
            meta['title'] = h1.group(1).strip()
    return meta, text


def find_rdr_file(rdr_path, id_str):
    m = re.search(r'\d+', id_str)
    if not m:
        return None
    num_int = int(m.group(0))
    for f in sorted(rdr_path.glob('*.md')):
        nums = re.findall(r'\d+', f.stem)
        if nums and int(nums[0]) == num_int:
            return f
    return None


EXCLUDED = {'readme.md', 'template.md', 'index.md', 'overview.md', 'workflow.md', 'templates.md'}

def get_all_rdrs(rdr_path):
    """Return list of dicts for all RDRs in the directory."""
    all_md = sorted(rdr_path.glob('*.md'))
    rdrs = []
    for f in all_md:
        if f.name.lower() in EXCLUDED:
            continue
        fm, text = parse_frontmatter(f)
        rtype = fm.get('type', '?')
        doc_status = fm.get('status', '?')
        if doc_status == '?' and rtype == '?':
            continue  # prose doc, not an RDR
        rdrs.append({
            'file': f.name,
            'path': f,
            'text': text,
            'title': fm.get('title', fm.get('name', f.stem)),
            'status': doc_status,
            'rtype': rtype,
            'priority': fm.get('priority', '?'),
        })
    return rdrs


if not rdr_path.exists():
    print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
    sys.exit(0)

id_match = re.search(r'\d+', args)

if not id_match:
    print("> **Usage**: `/nx:rdr-gate <id>` — e.g. `/nx:rdr-gate 003` or `/nx:rdr-gate RDR-003`")
    print()
    rdrs = get_all_rdrs(rdr_path)
    print("### Available RDRs")
    print()
    if rdrs:
        print("| File | Title | Status | Type |")
        print("|------|-------|--------|------|")
        for r in rdrs:
            print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
    else:
        print(f"No RDRs found in `{rdr_dir}`")
    sys.exit(0)

rdr_file = find_rdr_file(rdr_path, id_match.group(0))
if not rdr_file:
    print(f"> RDR not found for ID: `{id_match.group(0)}`")
    sys.exit(0)

fm, text = parse_frontmatter(rdr_file)
title = fm.get('title', fm.get('name', rdr_file.stem))
rdr_num = re.search(r'\d+', rdr_file.stem)
t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

print(f"### RDR File: {rdr_file.name}")
print(f"**Title:** {title}  **Status:** {fm.get('status', '?')}  **Type:** {fm.get('type', '?')}")
print()

# Strip fenced code blocks before extracting headings (avoids # comment false positives)
def strip_code_blocks(src):
    return re.sub(r'```.*?```', '', src, flags=re.DOTALL)


# Gap-structure pre-check (nexus-4qpb): the close skill enforces
# `#### Gap N: <title>` headings in Problem Statement for post-65 RDRs.
# Enforcing it only at close meant authors could gate, accept, and
# then discover the rule at the worst possible time. Shift left: block
# the gate here so missing gaps can't survive to close. Matches the
# close skill's regex and grandfathering threshold (id < 65).
_skip_gaps = '--skip-gaps' in args
_problem_idx = text.find('## Problem Statement')
if _problem_idx == -1:
    _problem_idx = text.find('## Problem')
_problem_section = ''
if _problem_idx != -1:
    _rest = text[_problem_idx:]
    _nxt = re.search(r'\n## ', _rest[1:])
    _problem_section = _rest[:_nxt.start() + 1] if _nxt else _rest
_gap_headings = re.findall(r'^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$', _problem_section, re.MULTILINE)
try:
    _rdr_id_int = int(t2_key)
except ValueError:
    _rdr_id_int = -1

if _rdr_id_int >= 65 and len(_gap_headings) == 0 and not _skip_gaps:
    print(f"> **BLOCKED** (Layer 1 — gap structure): RDR-{t2_key} has no `#### Gap N: <title>` "
          f"headings in `## Problem Statement` or `## Problem`.")
    print(r"> Expected format: `#### Gap 1: <gap title>` (regex: `^#{3,5} Gap \d+:`).")
    print(">")
    print("> The close skill enforces the same structure and will block `/nx:rdr-close "
          "--reason implemented`. Add the headings now before accept, or re-run the gate "
          "with `--skip-gaps` to record an intentional override in the audit trail.")
    sys.exit(0)
elif _rdr_id_int >= 65 and len(_gap_headings) > 0:
    print(f"#### Gap structure: {len(_gap_headings)} gap heading(s) present")
    print()
    for _num, _qual, _title in _gap_headings:
        _qual_str = _qual.strip()
        _qual_disp = f" {_qual_str}" if _qual_str else ""
        print(f"- Gap{_num}{_qual_disp}: {_title.strip()}")
    print()
elif _rdr_id_int < 65 and len(_gap_headings) == 0:
    print(f"> **Note**: RDR-{t2_key} predates the gap-structure convention (id < 65) — "
          "skipping the Layer 1 gap check.")
    print()

clean = strip_code_blocks(text)

# Section headings (structural completeness check — Layer 1)
headings = re.findall(r'^(#{1,3} .+)', clean, re.MULTILINE)
print("#### Section Structure (for completeness check)")
print()
for h in headings:
    print(h)
print()

# Section summaries: first non-empty, non-heading line of each ## section
print("#### Section Summaries")
print()
sections = re.split(r'^(## .+)', clean, flags=re.MULTILINE)
for i in range(1, len(sections) - 1, 2):
    heading = sections[i].strip()
    body = sections[i + 1]
    first_lines = [l.strip() for l in body.splitlines()
                   if l.strip() and not l.strip().startswith('#')]
    summary = first_lines[0][:120] if first_lines else '_empty_'
    print(f"**{heading}**: {summary}")
print()

# T2 metadata
print("### T2 Metadata")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" to retrieve T2 metadata.")
print()

# T2 research findings
print("### T2 Research Findings")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"\" to list all entries, then filter for {t2_key}-research* titles.")
print(f"If no research findings exist, run `/nx:rdr-research add {t2_key}` to record findings before gating.")
print(f"Use `--skip-research` in your gate command to override.")
PYEOF
}

## RDR to Gate

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Run all three gate layers in sequence:
  - **Layer 1 — Structural**: Use the Section Structure and Section Summaries above to check completeness (required headings present, no empty sections). **If no research findings exist** and `--skip-research` was NOT passed, report **BLOCKED** and stop — do not proceed to Layer 2 or 3. If `--skip-research` was passed, note the override and continue.
  - **Layer 2 — Assumption audit**: Use T2 Research Findings above to verify assumptions are evidenced. Every finding classified as "Assumed" must have an explicit risk assessment.
  - **Layer 3 — AI critique**: Dispatch the `substantive-critic` agent via Agent tool with the full RDR content. If the RDR has `related_issues` listing other RDR IDs, read those RDRs and include their content in the critique prompt — the critic should check for consistency and contradictions between related RDRs (P7).
- Gate outcomes: **BLOCKED** (critical issues found, must fix and re-gate) or **PASSED** (no critical issues). Do not use "Conditional Accept" or other ad-hoc outcomes.
- **Write T2 gate result** after completing all layers. Use the repo name from above:
  Use **memory_put** tool: project="{repo_name}_rdr", title="{id}-gate-latest", ttl="permanent", tags="rdr,gate", content with:
  ```
  outcome: "PASSED"  # or "BLOCKED"
  date: "YYYY-MM-DD"
  critical_count: 0
  significant_count: 2
  observation_count: 3
  summary: "One-sentence summary of gate result"
  ```
  This overwrites any previous gate result for this RDR, so only the latest gate run is stored.
- **If PASSED**, print: `> Run '/nx:rdr-accept <id>' to accept this RDR.`
- If no ID given, show the available RDR table above and prompt for an ID.
