---
description: Accept a gated RDR — verifies gate PASSED in T2, updates status to accepted
---

# RDR Accept

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
            continue
        rdrs.append({
            'file': f.name,
            'path': f,
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
    print("> **Usage**: `/rdr-accept <id>` — e.g. `/rdr-accept 003` or `/rdr-accept RDR-003`")
    print()
    rdrs = get_all_rdrs(rdr_path)
    draft_rdrs = [r for r in rdrs if r['status'].lower() == 'draft']
    print("### Draft RDRs (eligible for acceptance)")
    print()
    if draft_rdrs:
        print("| File | Title | Status | Type |")
        print("|------|-------|--------|------|")
        for r in draft_rdrs:
            print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
    else:
        print("No Draft RDRs found. Only Draft RDRs can be accepted.")
    sys.exit(0)

rdr_file = find_rdr_file(rdr_path, id_match.group(0))
if not rdr_file:
    print(f"> RDR not found for ID: `{id_match.group(0)}`")
    sys.exit(0)

fm, text = parse_frontmatter(rdr_file)
title = fm.get('title', fm.get('name', rdr_file.stem))
current_status = fm.get('status', '?')
rdr_num = re.search(r'\d+', rdr_file.stem)
t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

print(f"### RDR: {rdr_file.name}")
print(f"**Title:** {title}  **Current Status:** {current_status}")
print()

# Check current status — only Draft can be accepted
if current_status.lower() == 'accepted':
    print(f"> RDR is already accepted. Nothing to do.")
    sys.exit(0)
elif current_status.lower() not in ('draft',):
    print(f"> **BLOCKED**: RDR status is `{current_status}`. Only Draft RDRs can be accepted.")
    sys.exit(0)

# Check T2 gate result
print("### T2 Gate Result")
try:
    result = subprocess.run(
        ['nx', 'memory', 'get', '--project', f'{repo_name}_rdr', '--title', f'{t2_key}-gate-latest'],
        capture_output=True, text=True, timeout=10)
    gate_out = (result.stdout or '').strip()
    if gate_out:
        print(gate_out)
    else:
        print(f"No gate result found for RDR {t2_key}")
        print()
        print(f"> **BLOCKED**: No gate record in T2. Run `/rdr-gate {t2_key}` first.")
        sys.exit(0)
except Exception as exc:
    print(f"T2 not available: {exc}")
    print()
    print("> **Warning**: Cannot verify gate result. Proceeding based on file content.")
print()

# T2 metadata (for context)
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

print(f"**RDR file path:** `{rdr_file}`")
PYEOF
}

## RDR to Accept

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- **Verify gate**: Check that the T2 Gate Result above shows `outcome: "PASSED"`. If it shows `BLOCKED` or is missing, report **BLOCKED** and stop.
- **Self-healing check**: If T2 metadata already shows `status: "accepted"` but the file shows `draft`, update the file to match T2 (repair the file). Report the repair and stop.
- **Update T2 first** (T2 is the process authority):
  ```bash
  nx memory put - --project {repo_name}_rdr --title {id} --ttl permanent --tags rdr,{type} <<'EOF'
  ... (same fields from T2 Metadata above, with status: "accepted", accepted_date: "YYYY-MM-DD")
  EOF
  ```
- **Update the RDR file**: Change `status: draft` to `status: accepted` in the YAML frontmatter. Add `accepted_date: YYYY-MM-DD` if not present.
- **Update `reviewed-by`**: If `reviewed-by` is empty or placeholder, set to `self` (solo review).
- **Regenerate README**: Update `{rdr_dir}/README.md` index to reflect the new status.
- **Stage files**: `git add` the modified RDR file and README.
- Print confirmation: `> RDR {id} accepted. Ready for implementation or '/rdr-close {id} --reason implemented'.`
