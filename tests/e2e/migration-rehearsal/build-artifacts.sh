#!/usr/bin/env bash
# Build the release battery's artifacts ONCE (nexus-mfage fix B, item 1).
#
#   tests/e2e/migration-rehearsal/build-artifacts.sh <artifacts-dir> [--no-native]
#
# Produces, under <artifacts-dir>:
#   wheel/conexus-*.whl      the working-tree wheel
#   jar/nexus-service-*.jar  the stamped dev jar (local-service-gate's launch artifact)
#   native/nexus-service     the LINUX native candidate (+ its .so siblings)
#   manifest.json            tree identity + one sha256 per artifact + the stamp
#
# Every consumer (run.sh --artifacts, local-service-gate.sh --artifacts, the
# battery driver) verifies the manifest's tree identity against ITS checkout
# before touching a file, and refuses on mismatch — reuse is proven by
# identity, never by age (nexus-mbeke). release.properties is stamped ONCE
# here (release_version = REQUIRED_ENGINE_VERSION, build_ref = a per-BUILD
# nonce, nexus-308ph) under scripts/lib/build-lease.sh, and its pre-invocation
# bytes are restored on every exit path (nexus-iws18). The nonce is recorded
# in the manifest so each consuming leg can assert it against /version.
#
# The native build runs in the same GraalVM container as run.sh's own build
# (see that file for why: the binary must match the rehearsal image's OS, and
# -Pnative's jOOQ codegen needs the host Docker daemon). --no-native skips it
# for a wheel+jar-only battery.
set -euo pipefail
export NO_COLOR=1
unset FORCE_COLOR CLICOLOR_FORCE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/exit_diagnostics.sh disable=SC1091
source "$SCRIPT_DIR/../lib/exit_diagnostics.sh"
diag_arm_err_trap
trap 'diag_exit_guard' EXIT
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

OUT="${1:-}"
[ -n "$OUT" ] || { echo "usage: $0 <artifacts-dir> [--no-native]" >&2; exit 2; }
shift
WITH_NATIVE=1
for a in "$@"; do
  case "$a" in
    --no-native) WITH_NATIVE=0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

RELEASE_PROPS="service/src/main/resources/META-INF/nexus/release.properties"
[ -f "$RELEASE_PROPS" ] || { echo "FATAL: $RELEASE_PROPS missing" >&2; exit 2; }
RELEASE_VERSION="$(python3 -c '
import re, pathlib
src = pathlib.Path("src/nexus/engine_version.py").read_text()
m = re.search(r"REQUIRED_ENGINE_VERSION[^=]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", src)
print(".".join(m.groups()) if m else "")
')"
[ -n "$RELEASE_VERSION" ] || { echo "FATAL: could not parse REQUIRED_ENGINE_VERSION" >&2; exit 2; }

# Identity is taken BEFORE the stamp (the stamp file is excluded anyway).
IDENTITY_JSON="$(python3 "$SCRIPT_DIR/../lib/tree_identity.py" "$REPO_ROOT")"
echo "[artifacts] tree $(python3 -c 'import json,sys;d=json.loads(sys.argv[1]);print(d["tree_hash"][:12], "HEAD", d["head_sha"][:12], "dirty" if d["dirty"] else "clean", d["file_count"], "files")' "$IDENTITY_JSON")"

# shellcheck source=../../../scripts/lib/build-lease.sh disable=SC1091
source "$REPO_ROOT/scripts/lib/build-lease.sh"
build_lease_acquire service build-artifacts.sh "$OUT"
PROPS_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/release.properties.snapshot.XXXXXX")"
cp "$RELEASE_PROPS" "$PROPS_SNAPSHOT"
_restore() {
  cp "$PROPS_SNAPSHOT" "$RELEASE_PROPS" 2>/dev/null || true
  rm -f "$PROPS_SNAPSHOT"
  build_lease_release service 2>/dev/null || true
}
trap 'diag_exit_guard; _restore' EXIT

SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
BUILD_REF="${SHA}+$(date +%s)-$$"
grep -Ev '^release_version=|^build_ref=' "$PROPS_SNAPSHOT" > "$RELEASE_PROPS.tmp"
printf 'release_version=%s\nbuild_ref=%s\n' "$RELEASE_VERSION" "$BUILD_REF" >> "$RELEASE_PROPS.tmp"
mv "$RELEASE_PROPS.tmp" "$RELEASE_PROPS"
echo "[artifacts] stamped release_version=$RELEASE_VERSION build_ref=$BUILD_REF (bytes restored on exit)"

rm -rf "$OUT/wheel" "$OUT/jar" "$OUT/native" "$OUT/manifest.json"
mkdir -p "$OUT/wheel" "$OUT/jar"

echo "[artifacts] 1/3 wheel (host)…"
if ! uv build --wheel -o "$OUT/wheel" > "$OUT/wheel-build.log" 2>&1; then
  echo "uv build --wheel FAILED:" >&2; sed 's/^/    /' "$OUT/wheel-build.log" >&2; exit 1
fi
WHEEL="$(find "$OUT/wheel" -name 'conexus-*.whl' -type f | sort | tail -1)"
[ -n "$WHEEL" ] || { echo "no wheel produced under $OUT/wheel" >&2; exit 1; }

echo "[artifacts] 2/3 stamped dev jar (host mvnw, lease held)…"
( cd service && ./mvnw -q package -DskipTests ) > "$OUT/jar-build.log" 2>&1 \
  || { echo "jar build FAILED:" >&2; tail -40 "$OUT/jar-build.log" >&2; exit 1; }
JAR="$(find service/target -maxdepth 1 -name 'nexus-service-*.jar' ! -name 'original-*' -type f | sort | tail -1)"
[ -n "$JAR" ] || { echo "no jar under service/target" >&2; exit 1; }
cp "$JAR" "$OUT/jar/"

if [ "$WITH_NATIVE" = 1 ]; then
  echo "[artifacts] 3/3 LINUX native candidate (GraalVM container, ~2-3m)…"
  GRAAL_IMAGE="container-registry.oracle.com/graalvm/native-image-community:25"
  vm_mib=$(( $(docker info --format '{{.MemTotal}}') / 1048576 ))
  NATIVE_MAXHEAP="$(( vm_mib * 70 / 100 ))m"
  [ "$(( vm_mib * 70 / 100 ))" -lt 5632 ] && NATIVE_MAXHEAP=5632m
  rm -f service/target/nexus-service
  docker run --rm --entrypoint bash \
    --add-host=host.docker.internal:host-gateway \
    -v "$PWD":/src -w /src/service \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e TESTCONTAINERS_RYUK_DISABLED=true \
    -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
    "$GRAAL_IMAGE" \
    -c "./mvnw -B -Pnative -DskipTests -Dnative.image.opt=-Ob -Dnative.image.maxheap=${NATIVE_MAXHEAP} package" \
    > "$OUT/native-build.log" 2>&1 \
    || { echo "native build FAILED:" >&2; tail -40 "$OUT/native-build.log" >&2; exit 1; }
  [ -x service/target/nexus-service ] || { echo "native build produced no service/target/nexus-service" >&2; exit 1; }
  mkdir -p "$OUT/native"
  cp service/target/nexus-service "$OUT/native/"
  if compgen -G "service/target/*.so" > /dev/null; then cp service/target/*.so "$OUT/native/"; fi
else
  echo "[artifacts] 3/3 native candidate SKIPPED (--no-native)"
fi

python3 - "$OUT" "$IDENTITY_JSON" "$RELEASE_VERSION" "$BUILD_REF" "$WITH_NATIVE" <<'PY'
import hashlib, json, os, sys, time
out, identity, release_version, build_ref, with_native = sys.argv[1:6]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()
artifacts = {}
for name, sub in (("wheel", "wheel"), ("jar", "jar")):
    files = sorted(os.listdir(os.path.join(out, sub)))
    rel = f"{sub}/{files[-1]}"
    artifacts[name] = {"path": rel, "sha256": sha(os.path.join(out, rel))}
if with_native == "1":
    artifacts["native"] = {"path": "native/nexus-service", "sha256": sha(os.path.join(out, "native/nexus-service"))}
    for so in sorted(f for f in os.listdir(os.path.join(out, "native")) if f.endswith(".so")):
        artifacts[f"native/{so}"] = {"path": f"native/{so}", "sha256": sha(os.path.join(out, "native", so))}
manifest = dict(json.loads(identity))
manifest.update({
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "release_version": release_version,
    "build_ref": build_ref,
    "artifacts": artifacts,
})
with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"[artifacts] manifest.json: {len(artifacts)} artifacts, tree {manifest['tree_hash'][:12]}, build_ref {build_ref}")
PY
echo "ARTIFACTS BUILT: $OUT"
