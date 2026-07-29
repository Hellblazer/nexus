#!/usr/bin/env bash
# SQLite-T2 migration E2E — runs INSIDE a clean container against the develop
# wheel. Exercises this session's migration-runner work end-to-end:
#   RDR-170  registry-aware apply_pending + version stamp + doctor/dry-run
#   RDR-142  nx upgrade --dry-run reports deferred/gated steps + remediation
# (The nexus-3lbhb daemon-bootstrap scenario retired with the T2 daemon — see
#  the scenario-5 tombstone below.)
#
# Completely isolated: a fresh NEXUS_CONFIG_DIR per scenario, sqlite T2 backend,
# local mode — no service, no host contact.
set -uo pipefail

export NX_STORAGE_BACKEND=sqlite
# (RDR-155 P4b: the nexus-0rwwv bridge probe and its NX_MIGRATION_NOTICE
# kill switch are gone — no pin needed.)
export NX_LOCAL=1
SEED=/work/seed_gated.py
FAILS=0
PKGVER="$(python3 -c 'from importlib.metadata import version; print(version("conexus"))')"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS+1)); }
note() { printf '       %s\n' "$*"; }

# Set a fresh isolated config dir as $D AND export it (NOT in a subshell — a
# `D=$(fresh)` command-substitution would lose the export). nx resolves both the
# config dir and default_db_path from NEXUS_CONFIG_DIR (config.py).
fresh() { D="$(mktemp -d)"; export NEXUS_CONFIG_DIR="$D"; }
stored_ver() {
  python3 - "$1" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]+"/memory.db")
r=c.execute("SELECT value FROM _nexus_version WHERE key='cli_version'").fetchone()
print(r[0] if r else "NONE")
PY
}

note "container conexus wheel version: $PKGVER (registry-aware target should be >= this)"

# ── 1. Clean fresh upgrade WITH a catalog (RDR-170 apply_pending + RDR-142) ───
# A catalog-less fresh install correctly DEFERS the je0b PK steps (catalog
# absent) and never stamps the version — so the clean path needs a catalog for
# apply_pending to run to completion.
say "1 — clean fresh upgrade (catalog present)"
fresh; python3 "$SEED" "$D" catalog-only >/dev/null
nx upgrade >/tmp/up.txt 2>&1 && ok "nx upgrade exit 0" || { bad "nx upgrade failed"; cat /tmp/up.txt; }
nx upgrade --dry-run >/tmp/dry2.txt 2>&1
grep -qiE "No pending migrations|Up to date" /tmp/dry2.txt \
  && ok "post-upgrade dry-run clean ('No pending')" || { bad "post-upgrade dry-run not clean"; tail -8 /tmp/dry2.txt; }
nx doctor --check-schema >/tmp/doc.txt 2>&1
grep -qiE "passed|Schema version|OK|N/A" /tmp/doc.txt \
  && ok "nx doctor --check-schema healthy" || { bad "doctor --check-schema unhealthy"; cat /tmp/doc.txt; }

# ── 2. RDR-170: stamped version is the REGISTRY MAX, not the package version ──
say "2 — RDR-170 registry-aware stamped version"
SV="$(stored_ver "$D")"
note "package=$PKGVER  stored _nexus_version=$SV"
python3 - "$SV" "$PKGVER" <<'PY' && ok "stored schema version >= package (registry-aware)" || bad "stored version NOT registry-aware"
import sys
def t(v): return tuple(int(x) for x in v.split(".")[:3]) if v[0].isdigit() else (0,0,0)
sys.exit(0 if t(sys.argv[1]) >= t(sys.argv[2]) else 1)
PY

# ── 3. RDR-142: GATED dry-run reports the gate + remediation ─────────────────
say "3 — RDR-142 gated dry-run (orphan gate)"
fresh; python3 "$SEED" "$D" gated-orphan >/dev/null
NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD=1 nx upgrade --dry-run >/tmp/gate.txt 2>&1
note "$(grep -iE 'gate|table state|rename-collection|deferred' /tmp/gate.txt | head -3)"
grep -qi "No pending migrations" /tmp/gate.txt && bad "gated dry-run LIED ('No pending')" || ok "gated dry-run does NOT say 'No pending'"
grep -q "rename-collection" /tmp/gate.txt && ok "orphan remediation (rename-collection) present" || { bad "missing rename-collection remediation"; cat /tmp/gate.txt; }
grep -q "NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD" /tmp/gate.txt && ok "threshold-override remediation present" || bad "missing threshold remediation"
NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD=1 nx doctor --check-schema >/tmp/gatedoc.txt 2>&1
grep -qiE "pending|gate|orphan" /tmp/gatedoc.txt && ok "doctor --check-schema reports the gate" || { bad "doctor blind to the gate"; cat /tmp/gatedoc.txt; }

# ── 4. RDR-142: DEFERRED dry-run (catalog absent) ────────────────────────────
say "4 — RDR-142 deferred dry-run (catalog absent)"
fresh; python3 "$SEED" "$D" deferred >/dev/null
nx upgrade --dry-run >/tmp/defer.txt 2>&1
grep -qi "No pending migrations" /tmp/defer.txt && bad "deferred dry-run LIED ('No pending')" || ok "deferred dry-run does NOT say 'No pending'"
grep -qiE "defer|catalog" /tmp/defer.txt && ok "deferred/catalog condition reported" || { bad "deferral not reported"; cat /tmp/defer.txt; }

# ── 5. REMOVED: nexus-3lbhb T2 daemon bootstrap gate ─────────────────────────
# The scenario started `nx daemon t2 start` on a gated db and asserted the
# bootstrap hit the migration gate FAIL-CLOSED (crash, not serve-degraded).
# Both the verb and `run_t2_daemon` retired in nexus-i711w Stage 2 sub-stage B,
# so there is no bootstrap left to gate. Caught by
# tests/test_release_artifact_verb_rot.py, which is exactly what that tripwire
# exists for — the script is release-only and nothing else would have noticed.
# Scenarios 1-4 (RDR-170 apply_pending + RDR-142 dry-run reporting) are
# unaffected: they drive `nx upgrade`, not the daemon.

say "RESULT"
if [ "$FAILS" -eq 0 ]; then printf '\033[32mALL SCENARIOS PASSED\033[0m\n'; exit 0
else printf '\033[31m%d SCENARIO CHECK(S) FAILED\033[0m\n' "$FAILS"; exit 1; fi
