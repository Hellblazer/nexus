#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Live smoke-test for RDR-078 (plan-centric retrieval) + RDR-080 (nx_answer).
#
# Runs every changed surface in an isolated sandbox — your prod T2, T3, and
# catalog are untouched. Uses local-mode ChromaDB (ONNX MiniLM) so no API
# keys are needed. Run from the repo root.
#
# Exit code: 0 = all passed. Non-zero = a surface failed (check output).

set -euo pipefail

# ── Sandbox ──────────────────────────────────────────────────────────────────
SANDBOX="${SANDBOX:-/tmp/nx-validate-$$}"
mkdir -p "$SANDBOX"
trap '[[ "${KEEP_SANDBOX:-0}" == "1" ]] || rm -rf "$SANDBOX"' EXIT

# Redirect every nexus path into the sandbox — no collision with prod.
export HOME="$SANDBOX"           # T2 → $SANDBOX/.config/nexus/memory.db
export NX_LOCAL=1                # Force local mode (no Voyage/Chroma Cloud)
export NX_LOCAL_CHROMA_PATH="$SANDBOX/.local/share/nexus/chroma"
export NEXUS_CATALOG_PATH="$SANDBOX/.config/nexus/catalog"
mkdir -p "$SANDBOX/.config/nexus" "$SANDBOX/.local/share/nexus"

# Ensure our sandbox-HOME doesn't break git (it reads ~/.gitconfig)
cp "${ORIG_HOME:-$HOME}/.gitconfig" "$SANDBOX/.gitconfig" 2>/dev/null || true

echo "▶ Sandbox: $SANDBOX"
echo "▶ Branch:  $(git rev-parse --abbrev-ref HEAD)  ($(git rev-parse --short HEAD))"
echo

# ── Helpers ──────────────────────────────────────────────────────────────────
PASS=0
FAIL=0
pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
step() { echo; echo "━━━ $1 ━━━"; }

# ── 1. Catalog setup seeds 14 templates (5 legacy + 9 YAML) ──────────────────
step "1. Catalog setup + plan seeding"
SETUP_OUT=$(uv run nx catalog setup 2>&1)
echo "$SETUP_OUT" | tail -5

if uv run python - <<'PYEOF'
# Distinguish: legacy (RDR-063) → dimensions IS NULL
#              YAML   (RDR-078) → dimensions IS NOT NULL
from nexus.commands._helpers import default_db_path
from nexus.db.t2 import T2Database

with T2Database(default_db_path()) as db:
    all_plans = db.list_plans(limit=50)
    yaml_seeds = sum(1 for p in all_plans if p.get("dimensions"))
    legacy    = len(all_plans) - yaml_seeds
    print(f"  total: {len(all_plans)}   legacy (no dims): {legacy}   yaml (has dims): {yaml_seeds}")
    assert len(all_plans) == 14, f"expected 14 total, got {len(all_plans)}"
    assert legacy == 5, f"expected 5 legacy, got {legacy}"
    assert yaml_seeds == 9, f"expected 9 YAML, got {yaml_seeds}"
PYEOF
then pass "14 plans seeded (5 legacy + 9 YAML scenarios)"; else fail "plan library state"; fi

# ── 2. plan_match returns a hit for a research-shaped question ───────────────
step "2. plan_match semantic retrieval (FTS5 path)"
if uv run python - <<'PYEOF'
# Use a query that has words in the seeded plan descriptions.
# The research template says "Design / architecture / planning. Walk from
# a concept into the prose corpus..." — so "design planning corpus" hits.
from nexus.commands._helpers import default_db_path
from nexus.db.t2 import T2Database
from nexus.plans.matcher import plan_match

with T2Database(default_db_path()) as db:
    matches = plan_match(
        intent="design planning corpus",
        library=db.plans,
        cache=None,
        min_confidence=0.85,
        n=5,
    )
    print(f"  matches: {len(matches)}")
    for m in matches[:3]:
        row = db.plans.get_plan(m.plan_id)
        name = (row or {}).get("name") or "?"
        verb = (row or {}).get("verb") or "?"
        print(f"    - plan {m.plan_id}: verb={verb} name={name} conf={m.confidence}")
    assert len(matches) >= 1, "expected at least one FTS5 match"
    assert all(m.confidence is None for m in matches), "FTS5 path must have confidence=None"
PYEOF
then pass "plan_match returned a match via FTS5"; else fail "plan_match"; fi

# ── 3. traverse silent-drop contract ─────────────────────────────────────────
step "3. traverse — malformed seed handling"
if uv run python - <<'PYEOF'
from nexus.mcp.core import traverse

result = traverse(seeds=["not-a-tumbler", "bad"], link_types=["cites"])
assert isinstance(result, dict), f"expected dict, got {type(result)}"
assert result.get("tumblers") == [], f"expected empty tumblers, got {result}"
print(f"  all-malformed seeds → {result}")

result2 = traverse(seeds=["1.1"], link_types=["cites"], purpose="documentation-for")
assert "error" in result2, f"expected error, got {result2}"
print(f"  mutual-exclusion → error: {result2['error'][:60]}")
PYEOF
then pass "traverse handles malformed seeds"; else fail "traverse"; fi

# ── 4. store_get_many batch hydration at quota boundary ──────────────────────
step "4. store_get_many — 301-ID boundary (ChromaDB quota)"
if uv run python - <<'PYEOF'
from unittest.mock import MagicMock, patch
from nexus.mcp.core import store_get_many

mock_t3 = MagicMock()
store = {f"doc-{i:04d}": {"content": f"body-{i}"} for i in range(301)}
mock_t3.get_by_id = lambda col, doc_id: store.get(doc_id)

with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
    result = store_get_many(
        ids=[f"doc-{i:04d}" for i in range(301)],
        collections="knowledge",
        structured=True,
    )
assert len(result["contents"]) == 301, f"expected 301, got {len(result['contents'])}"
print(f"  301 IDs → {len(result['contents'])} contents, {len(result['missing'])} missing")
PYEOF
then pass "store_get_many crosses 300-ID quota boundary"; else fail "store_get_many"; fi

# ── 5. derive_title initialism preservation ──────────────────────────────────
step "5. derive_title — filename normalisation (nexus-8l6)"
if uv run python - <<'PYEOF'
from pathlib import Path
from nexus.indexer_utils import derive_title

cases = [
    ("my_api_v2.md", None, "My API V2"),
    ("rdr-078-plan-centric.md", None, "RDR 078 Plan Centric"),
    ("foo.md", "# Real Title\n\nbody", "Real Title"),
    ("carpenter.pdf", None, "Carpenter"),
]
for filename, body, expected in cases:
    got = derive_title(Path(filename), body)
    assert got == expected, f"{filename}: expected {expected!r}, got {got!r}"
    print(f"  {filename:30s} → {got}")
PYEOF
then pass "derive_title preserves initialisms"; else fail "derive_title"; fi

# ── 6. nx_answer orchestration (mocked I/O, real T2, real plan_match) ────────
step "6. nx_answer — end-to-end trunk with real FTS5"
if uv run python - <<'PYEOF'
import asyncio
from unittest.mock import AsyncMock, patch
from nexus.plans.runner import PlanResult

async def _run():
    import nexus.plans.runner as _runner
    with patch.object(_runner, "plan_run",
                      AsyncMock(return_value=PlanResult(steps=[{"text": "mocked answer"}]))):
        from nexus.mcp.core import nx_answer
        result = await nx_answer(
            "review the recent change set for decision drift",
            scope="global",
        )
    assert isinstance(result, str) and len(result) > 0
    print(f"  nx_answer returned: {result[:80]}...")

asyncio.run(_run())
PYEOF
then pass "nx_answer trunk executes (plan_match → run → record)"; else fail "nx_answer"; fi

# ── 7. Catalog graph_many node cap ───────────────────────────────────────────
step "7. graph_many — node cap + no dangling edges"
if uv run python - <<'PYEOF'
from pathlib import Path
from unittest.mock import MagicMock, patch
import os
from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler

cat_path = Path(os.environ["NEXUS_CATALOG_PATH"])
cat = Catalog(cat_path, cat_path / ".catalog.db")

def _node(s):
    n = MagicMock(); n.tumbler = Tumbler.parse(s); return n
def _edge(f, t):
    e = MagicMock()
    e.from_tumbler = Tumbler.parse(f); e.to_tumbler = Tumbler.parse(t)
    e.link_type = "cites"; return e

def fake_graph(seed, **kwargs):
    return {
        "nodes": [_node(f"1.0.{i}") for i in range(501)],
        "edges": [_edge("1.0.0", "1.0.500")],  # dangling after cap
    }

with patch.object(cat, "graph", side_effect=fake_graph):
    result = cat.graph_many([Tumbler.parse("1.1")], depth=1)

assert len(result["nodes"]) == 500, f"expected 500, got {len(result['nodes'])}"
node_keys = {str(n.tumbler) for n in result["nodes"]}
for edge in result["edges"]:
    assert str(edge.from_tumbler) in node_keys
    assert str(edge.to_tumbler) in node_keys
print(f"  {len(result['nodes'])} nodes (capped), {len(result['edges'])} edges (all live)")
PYEOF
then pass "graph_many cap + dangling-edge invariant"; else fail "graph_many"; fi

# ── 8. YAML template schema (CI gate parity) ─────────────────────────────────
step "8. Builtin YAML templates pass validate_plan_template"
if uv run python - <<'PYEOF'
import yaml
from pathlib import Path
from nexus.plans.schema import validate_plan_template, canonical_dimensions_json

builtin = Path(".").resolve() / "nx" / "plans" / "builtin"
files = sorted(builtin.glob("*.yml"))
assert len(files) == 9, f"expected 9 builtin files, got {len(files)}"

canonicals = {}
for f in files:
    raw = yaml.safe_load(f.read_text())
    validate_plan_template(raw)
    c = canonical_dimensions_json(raw["dimensions"])
    assert c not in canonicals, f"dim collision between {f.name} and {canonicals[c]}"
    canonicals[c] = f.name
    print(f"  {f.name:35s} → {raw['dimensions']}")
PYEOF
then pass "all 9 builtin templates validate"; else fail "template schema"; fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Sandbox: $SANDBOX  (KEEP_SANDBOX=1 to preserve)"
echo "  PASS: $PASS   FAIL: $FAIL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[[ $FAIL -eq 0 ]] || exit 1
