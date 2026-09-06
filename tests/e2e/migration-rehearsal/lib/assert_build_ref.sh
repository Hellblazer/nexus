#!/usr/bin/env bash
# Sourced inside a rehearsal container. assert_build_ref [label] asserts the
# RUNNING service's /version build_ref equals $EXPECT_BUILD_REF, the nonce
# tests/e2e/migration-rehearsal/build-artifacts.sh stamped into the candidate
# binary and recorded in its manifest (nexus-mfage, extending nexus-308ph
# from per-run to per-build). A missing or different value means the process
# serving this leg is not the candidate the manifest describes. No-op when
# EXPECT_BUILD_REF is unset (a run without --artifacts stamps nothing).
# Expects the caller's ok/bad helpers; falls back to echo + return 1.
assert_build_ref() {
  local label="${1:-build_ref}"
  [ -n "${EXPECT_BUILD_REF:-}" ] || return 0
  local lease got
  lease="$(ls "${NEXUS_CONFIG_DIR:-$HOME/.config/nexus}"/storage_service_addr.* 2>/dev/null | sort | tail -1)"
  got="$(python3 - "$lease" <<'PY'
import json, sys, urllib.request
lease = json.load(open(sys.argv[1]))
ep = lease.get("endpoint", lease)
req = urllib.request.Request(
    f"http://{ep.get('host', '127.0.0.1')}:{ep['port']}/version",
    headers={"Authorization": f"Bearer {ep['token']}"})
print(json.load(urllib.request.urlopen(req, timeout=10)).get("build_ref") or "")
PY
)"
  if [ "$got" = "$EXPECT_BUILD_REF" ]; then
    if declare -F ok >/dev/null; then ok "$label: /version build_ref=$got matches the artifact manifest"; else echo "PASS $label build_ref=$got"; fi
    return 0
  fi
  if declare -F bad >/dev/null; then bad "$label: /version build_ref='$got', manifest expects '$EXPECT_BUILD_REF' — the served process is not the manifest's candidate"; else echo "FAIL $label build_ref='$got' expected '$EXPECT_BUILD_REF'" >&2; fi
  return 1
}
