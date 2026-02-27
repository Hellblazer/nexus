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
    else:
        m = re.search(r'^## Metadata\s*\n(.*?)(?=^##|\Z)', text, re.MULTILINE | re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                kv = re.match(r'-?\s*\*\*(\w[\w\s]*?)\*\*:\s*(.+)', line.strip())
                if kv:
                    meta[kv.group(1).strip().lower()] = kv.group(2).strip()
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
    print("> **Usage**: `/rdr-gate <id>` — e.g. `/rdr-gate 003` or `/rdr-gate RDR-003`")
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
try:
    result = subprocess.run(
        ['nx', 'memory', 'get', '--project', f'{repo_name}_rdr', '--title', t2_key],
        capture_output=True, text=True, timeout=10)
    t2_out = (result.stdout or '').strip()
    print(t2_out if t2_out else f"No T2 record for RDR {t2_key}")
except Exception as exc:
    print(f"T2 not available: {exc}")
print()

# T2 research findings
print("### T2 Research Findings")
try:
    result = subprocess.run(
        ['nx', 'memory', 'list', '--project', f'{repo_name}_rdr'],
        capture_output=True, text=True, timeout=10)
    list_out = (result.stdout or '').strip()
    research_lines = [l for l in list_out.splitlines()
                      if re.match(rf'^{t2_key}-research', l)]
    if research_lines:
        print('\n'.join(research_lines))
    else:
        print("No research findings recorded")
        print()
        print("> **Layer 1 check**: No research findings exist for this RDR.")
        print(f"> Run `/rdr-research add {t2_key}` to record findings before gating.")
        print("> Use `--skip-research` in your gate command to override.")
except Exception as exc:
    print(f"T2 not available: {exc}")
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
  - **Layer 3 — AI critique**: Dispatch the `substantive-critic` agent via Task tool with the full RDR content. If the RDR has `related_issues` listing other RDR IDs, read those RDRs and include their content in the critique prompt — the critic should check for consistency and contradictions between related RDRs (P7).
- Gate outcomes: **BLOCKED** (critical issues found, must fix and re-gate) or **PASSED** (no critical issues). Do not use "Conditional Accept" or other ad-hoc outcomes.
- If no ID given, show the available RDR table above and prompt for an ID.
