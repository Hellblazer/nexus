#!/usr/bin/env bash
# Benchmark teardown (nexus-duoak.3): delete ONLY the scratch collections
# this benchmark created, and surface anything unexpected.
#
# Scoping logic lives in teardown_scope.py (unit-tested): a collection is
# deletable iff it is NEW (absent from the before-snapshot) AND owned by
# a benchidx-* catalog owner. Deletion goes through `nx collection
# delete` so taxonomy/catalog state cascades.
#
# Usage: teardown.sh <work-root> <out-dir>

set -euo pipefail

WORK_ROOT="${1:?work-root required}"
OUT_DIR="${2:?out-dir required}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$OUT_DIR/collections-before.txt" ]]; then
    echo "teardown: no before-snapshot in $OUT_DIR — refusing to delete anything" >&2
    exit 1
fi

uv run nx collection list > "$OUT_DIR/collections-after.txt"
uv run nx catalog owners --json > "$OUT_DIR/owners.json"

PYTHONPATH="$BENCH_DIR" uv run python - "$OUT_DIR" <<'EOF'
import json
import re
import sys
from pathlib import Path

from teardown_scope import bench_tumblers, plan_teardown

out = Path(sys.argv[1])
_NAME = re.compile(r"\b(\w+__[A-Za-z0-9_-]+__[a-z0-9-]+__v\d+)\b")

def names(p: Path) -> list[str]:
    return _NAME.findall(p.read_text())

owners = json.loads((out / "owners.json").read_text())
tumblers = bench_tumblers(owners)
to_delete, unexpected = plan_teardown(
    names(out / "collections-before.txt"),
    names(out / "collections-after.txt"),
    tumblers,
)
(out / "teardown-plan.txt").write_text("\n".join(to_delete) + "\n")
if unexpected:
    print(f"teardown: UNEXPECTED new non-bench collections (NOT deleting): {unexpected}", file=sys.stderr)
    sys.exit(2)
EOF

DELETED=0
while IFS= read -r coll; do
    [[ -z "$coll" ]] && continue
    echo "teardown: deleting $coll"
    uv run nx collection delete "$coll" --yes || echo "WARN: delete failed for $coll — remove manually" >&2
    DELETED=$((DELETED + 1))
done < "$OUT_DIR/teardown-plan.txt"
echo "teardown: done ($DELETED bench collections removed; plan in $OUT_DIR/teardown-plan.txt)"
