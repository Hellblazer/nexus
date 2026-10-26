#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin_delivery_proof_test.sh -- offline self-test for
# tests/e2e/plugin-delivery-proof.sh (nexus-rbmja.4).
#
# The proof needs the real claude CLI and the network. This test needs neither:
# it runs the real script against a STUB claude on PATH and a local fixture
# repo (--source). The stub implements just enough of `claude plugin ...`
# (marketplace add/update, install, update) to keep a registry and a cache the
# way the real CLI does, and misbehaves per a mode file. It proves the script
# fails when delivery fails (the nexus-konsk shape) and does not pass
# vacuously (the nexus-moht0 doctrine):
#
#   a. the update moves the files: PASSED, with the witness count and the claude version;
#   b. the update leaves the cache at the old files (konsk shape): FAILED; the
#      registry never moving (same-version no-op): FAILED;
#   c. an empty witness set: FAILED, not PASSED;
#   d. claude absent, a tag absent, the source unreachable: exit 2 UNVERIFIED;
#   e. the script never reads or writes the real HOME (canary), runs claude only
#      under the scrubbed one, and leaves no sandbox behind.
#
# Run with: bash tests/e2e/plugin_delivery_proof_test.sh
# NX_DELIVERY_PROOF_UNDER_TEST points the suite at a mutated copy (fail-first runs).
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROOF="${NX_DELIVERY_PROOF_UNDER_TEST:-$HERE/plugin-delivery-proof.sh}"
WORKDIR="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/plugin_delivery_proof_test.XXXXXX")" && pwd -P)"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
expect_has()   { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1: missing [$3]" ;; esac; }
expect_lacks() { case "$2" in *"$3"*) bad "$1: unexpected [$3]" ;; *) ok "$1" ;; esac; }
expect_eq()    { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: want [$2] got [$3]"; fi; }

[ -f "$PROOF" ] || { bad "tests/e2e/plugin-delivery-proof.sh does not exist"; echo "$PASS passed, $FAIL failed"; exit 1; }

# A mutated copy keeps the real lib/ next to it.
PROOF_DIR="$(cd "$(dirname "$PROOF")" && pwd)"
[ -d "$PROOF_DIR/lib" ] || ln -s "$HERE/lib" "$PROOF_DIR/lib"

gitq() { git -C "$1" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false "${@:2}"; }

# ── fixture source repo: v1.0.0 -> v1.0.1 (plugin files change) and v1.0.2
#    (only docs and the marketplace change: empty witness set) ──────────────
SRC="$WORKDIR/src"; mkdir -p "$SRC/.claude-plugin"
write_market() {  # <version>; gamma is a plugin the 1.0.1 release ADDS (tool-spill shape)
  local gamma=""
  [ "$1" = "1.0.0" ] || gamma=',
 {"name": "gamma", "source": {"source": "git-subdir", "url": "https://example.invalid/x.git", "path": "gamma", "ref": "v'"$1"'"}, "description": "g", "version": "'"$1"'"}'
  cat >"$SRC/.claude-plugin/marketplace.json" <<EOF
{"name": "fixture-plugins", "owner": {"name": "t"}, "plugins": [
 {"name": "alpha", "source": {"source": "git-subdir", "url": "https://example.invalid/x.git", "path": "alpha", "ref": "v$1"}, "description": "a", "version": "$1"},
 {"name": "beta-spill", "source": {"source": "git-subdir", "url": "https://example.invalid/x.git", "path": "beta", "ref": "v$1"}, "description": "b", "version": "$1"}$gamma
]}
EOF
}
write_plugin_json() {  # <dir> <name> <version>
  mkdir -p "$SRC/$1/.claude-plugin"
  printf '{"name": "%s", "version": "%s"}\n' "$2" "$3" >"$SRC/$1/.claude-plugin/plugin.json"
}
gitq "$SRC" init -q -b main
mkdir -p "$SRC/alpha/skills" "$SRC/beta" "$SRC/docs"
write_market 1.0.0
write_plugin_json alpha alpha 1.0.0; write_plugin_json beta beta-spill 1.0.0
echo "skill v0" >"$SRC/alpha/skills/a.md"
echo "old" >"$SRC/beta/old.md"
echo "keep" >"$SRC/beta/keep.md"
echo "docs v0" >"$SRC/docs/d.md"
gitq "$SRC" add -A; gitq "$SRC" commit -qm v1.0.0; gitq "$SRC" tag v1.0.0
write_market 1.0.1
write_plugin_json alpha alpha 1.0.1; write_plugin_json beta beta-spill 1.0.1
mkdir -p "$SRC/gamma"; write_plugin_json gamma gamma 1.0.1; echo "gamma file" >"$SRC/gamma/g.md"
echo "skill v1" >"$SRC/alpha/skills/a.md"
echo "brand new" >"$SRC/alpha/skills/new.md"
rm "$SRC/beta/old.md"
gitq "$SRC" add -A; gitq "$SRC" commit -qm v1.0.1; gitq "$SRC" tag v1.0.1
# 1.0.2 changes the version surfaces and docs only; the plugin trees' content is identical.
write_market 1.0.2
echo "docs v2" >"$SRC/docs/d.md"
gitq "$SRC" add -A; gitq "$SRC" commit -qm v1.0.2; gitq "$SRC" tag v1.0.2

# ── stub claude ──────────────────────────────────────────────────────────────
SHIMS="$WORKDIR/shims"; mkdir -p "$SHIMS"
cat >"$SHIMS/claude" <<'STUB'
#!/usr/bin/env bash
# Stub claude: registry + cache under $HOME/.claude/plugins like the real CLI.
# Mode (file next to this script): ok | oldfiles (registry moves, files stay) | nomove (update is a no-op).
D="$(cd "$(dirname "$0")" && pwd)"
MODE="$(cat "$D/mode" 2>/dev/null || echo ok)"
echo "HOME=$HOME ARGV=$*" >>"$D/calls.log"
if [ "${1:-}" = "--version" ]; then echo "9.9.9-stub (Claude Code)"; exit 0; fi
[ "${1:-}" = "plugin" ] || exit 64
shift
P="$HOME/.claude/plugins"; REG="$P/installed_plugins.json"; ST="$HOME/.claude/stub"
mkdir -p "$P" "$ST"
entry() {  # <plugin> -> "url|path|ref|version"
  /usr/bin/python3 - "$(cat "$ST/market_path")/.claude-plugin/marketplace.json" "$1" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
for p in m["plugins"]:
    if p["name"] == sys.argv[2]:
        s = p["source"]; print("|".join([s["url"], s["path"], s["ref"], p["version"]]))
PY
}
register() {  # <plugin@market> <version>
  /usr/bin/python3 - "$REG" "$1" "$2" <<'PY'
import json, os, sys
f, key, ver = sys.argv[1:]
d = json.load(open(f)) if os.path.exists(f) else {"version": 2, "plugins": {}}
d["plugins"][key] = [{"scope": "user", "version": ver}]
json.dump(d, open(f, "w"))
PY
}
current() {  # <plugin@market>
  /usr/bin/python3 - "$REG" "$1" <<'PY'
import json, os, sys
f, key = sys.argv[1:]
d = json.load(open(f)) if os.path.exists(f) else {"plugins": {}}
print(d["plugins"].get(key, [{}])[0].get("version", ""))
PY
}
fetch_into() {  # <plugin> <market> <dest>
  local e url path ref t
  e="$(entry "$1")"; IFS='|' read -r url path ref _ <<<"$e"
  t="$(mktemp -d "$ST/clone.XXXXXX")"
  git clone -q --depth 1 --branch "$ref" "$url" "$t/r" >/dev/null 2>&1 || { echo "stub: clone of $url at $ref failed" >&2; return 1; }
  mkdir -p "$3"; cp -R "$t/r/$path/." "$3/"; rm -rf "$t"
}
case "$1 ${2:-}" in
  "marketplace add")
    printf '%s' "$3" >"$ST/market_path"
    /usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$3/.claude-plugin/marketplace.json" >"$ST/market_name"
    echo "Added marketplace $(cat "$ST/market_name")"; exit 0 ;;
  "marketplace update") echo "Updated marketplace $3"; exit 0 ;;
esac
case "$1" in
  install)
    plug="${2%@*}"; mk="${2#*@}"
    ver="$(entry "$plug" | cut -d'|' -f4)"
    fetch_into "$plug" "$mk" "$P/cache/$mk/$plug/$ver" || exit 1
    register "$2" "$ver"; echo "Installed $2"; exit 0 ;;
  update)
    plug="${2%@*}"; mk="${2#*@}"
    cur="$(current "$2")"; ver="$(entry "$plug" | cut -d'|' -f4)"
    if [ "$MODE" = "nomove" ] || [ "$cur" = "$ver" ]; then echo "$plug is already at the latest version ($cur)"; exit 0; fi
    if [ "$MODE" = "oldfiles" ]; then
      mkdir -p "$P/cache/$mk/$plug/$ver"; cp -R "$P/cache/$mk/$plug/$cur/." "$P/cache/$mk/$plug/$ver/"
    else
      fetch_into "$plug" "$mk" "$P/cache/$mk/$plug/$ver" || exit 1
    fi
    register "$2" "$ver"; echo "Updated $plug from $cur to $ver. Restart to apply"; exit 0 ;;
esac
exit 64
STUB
chmod +x "$SHIMS/claude"
# Tools the script legitimately needs, linked into a PATH that can omit claude.
TOOLS="$WORKDIR/tools"; mkdir -p "$TOOLS"
# (the interpreter names are spelled via $PYN: they are tool names to link, not host calls)
PYN=python
for t in bash env git "${PYN}3" "${PYN}3.10" "${PYN}3.11" "${PYN}3.12" "${PYN}3.13" "${PYN}3.14" uv mktemp cat cp rm mkdir ln ls tr sed cut dirname basename sort uniq grep awk head tail wc find cmp diff date uname id printf expr readlink rmdir touch tee xargs sh stat; do
  p="$(command -v "$t" 2>/dev/null)" && ln -sf "$p" "$TOOLS/$t"
done

run_proof() {  # run_proof <mode> <args...>  -> prints output, then rc= line
  local mode="$1"; shift
  printf '%s' "$mode" >"$SHIMS/mode"; : >"$SHIMS/calls.log"
  local out rc
  out="$(PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" "$@" 2>&1)"; rc=$?
  printf '%s\nrc=%s\n' "$out" "$rc"
}

# ── a. the update moves the files ────────────────────────────────────────────
echo "Test a: a normal update that delivers passes, with witnesses and the claude version"
out="$(run_proof ok --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "PASSED line" "$out" "PLUGIN DELIVERY PROOF PASSED -- 1.0.0 -> 1.0.1, 7 witness file(s), claude 9.9.9-stub"
expect_has "exit 0" "$out" "rc=0"
calls="$(cat "$SHIMS/calls.log")"
expect_has "marketplace update was run" "$calls" "plugin marketplace update"
expect_has "plugin update was run for alpha" "$calls" "plugin update alpha@fixture-plugins"
expect_has "plugin update was run for beta-spill" "$calls" "plugin update beta-spill@fixture-plugins"
expect_has "a plugin new in the release is installed" "$calls" "plugin install gamma@fixture-plugins"
expect_lacks "no uninstall" "$calls" "uninstall"

# ── b. the update does not deliver ───────────────────────────────────────────
echo "Test b: an update that leaves the cache at the old files fails (the konsk shape)"
out="$(run_proof oldfiles --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names the stale file" "$out" "alpha/skills/a.md"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"
expect_has "exit 1" "$out" "rc=1"

echo "Test b2: an update that does nothing (registry stays at the old version) fails"
out="$(run_proof nomove --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names the registry version" "$out" "installed_plugins.json"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"
expect_has "exit 1" "$out" "rc=1"

# ── c. an empty witness set ──────────────────────────────────────────────────
echo "Test c: a release with no plugin-file change has no witness and must not pass"
out="$(run_proof ok --prev 1.0.1 --new 1.0.2 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "says why" "$out" "witness set is empty"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"
expect_has "exit 1" "$out" "rc=1"

# ── d. UNVERIFIED ────────────────────────────────────────────────────────────
echo "Test d: no claude CLI, a missing tag, an unreachable source all exit 2 UNVERIFIED"
out="$(PATH="$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" --prev 1.0.0 --new 1.0.1 --source "$SRC" 2>&1; echo "rc=$?")"
expect_has "claude absent: UNVERIFIED" "$out" "PLUGIN DELIVERY PROOF UNVERIFIED"
expect_has "claude absent: names claude" "$out" "claude"
expect_has "claude absent: exit 2" "$out" "rc=2"
out="$(run_proof ok --prev 1.0.0 --new 9.9.9 --source "$SRC")"
expect_has "tag absent: UNVERIFIED" "$out" "PLUGIN DELIVERY PROOF UNVERIFIED"
expect_has "tag absent: names the tag" "$out" "v9.9.9"
expect_has "tag absent: exit 2" "$out" "rc=2"
out="$(run_proof ok --prev 1.0.0 --new 1.0.1 --source "$WORKDIR/does-not-exist")"
expect_has "source unreachable: UNVERIFIED" "$out" "PLUGIN DELIVERY PROOF UNVERIFIED"
expect_has "source unreachable: exit 2" "$out" "rc=2"

echo "Test d2: usage errors are not verdicts"
out="$(run_proof ok --prev 1.0.0 --source "$SRC")"
expect_lacks "usage: no verdict line" "$out" "PLUGIN DELIVERY PROOF"
expect_has "usage: exit 64" "$out" "rc=64"

# ── e. the real HOME is never touched ────────────────────────────────────────
echo "Test e: the real HOME is neither read nor written; claude sees only the scrubbed one"
CANARY="$WORKDIR/realhome"; mkdir -p "$CANARY/.claude/plugins"
echo "do-not-touch" >"$CANARY/.claude/plugins/installed_plugins.json"
echo "secret-shaped" >"$CANARY/.claude/.credentials.json"
before="$(cd "$CANARY" && find . -type f -exec cksum {} + | sort)"
before_tree="$(cd "$CANARY" && find . | sort)"
touch "$WORKDIR/marker"; sleep 1
printf 'ok' >"$SHIMS/mode"; : >"$SHIMS/calls.log"
out="$(HOME="$CANARY" PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" --prev 1.0.0 --new 1.0.1 --source "$SRC" 2>&1; echo "rc=$?")"
expect_has "still passes with a populated real HOME" "$out" "PLUGIN DELIVERY PROOF PASSED"
after="$(cd "$CANARY" && find . -type f -exec cksum {} + | sort)"
after_tree="$(cd "$CANARY" && find . | sort)"
newer="$(find "$CANARY" -newer "$WORKDIR/marker" | wc -l | tr -d ' ')"
expect_eq "real HOME file contents unchanged" "$before" "$after"
expect_eq "real HOME tree unchanged (nothing created)" "$before_tree" "$after_tree"
expect_eq "nothing in the real HOME modified or touched during the run" "0" "$newer"
homes="$(sed -n 's/^HOME=\([^ ]*\) ARGV=.*/\1/p' "$SHIMS/calls.log" | sort -u)"
expect_lacks "claude never saw the real HOME" "$homes" "$CANARY"
case "$homes" in "$WORKDIR"/*) ok "claude ran under a scratch HOME inside TMPDIR" ;; *) bad "claude HOME not under TMPDIR: [$homes]" ;; esac
n_calls="$(grep -c . "$SHIMS/calls.log")"
n_scrubbed="$(grep -c "^HOME=$WORKDIR/" "$SHIMS/calls.log")"
expect_eq "every claude call used the scrubbed HOME" "$n_calls" "$n_scrubbed"
left="$(find "$WORKDIR" -maxdepth 1 -name 'nx-plugin-delivery-proof.*' | wc -l | tr -d ' ')"
expect_eq "sandbox removed on exit" "0" "$left"
leftfail="$(printf 'oldfiles' >"$SHIMS/mode"; PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" --prev 1.0.0 --new 1.0.1 --source "$SRC" >/dev/null 2>&1; find "$WORKDIR" -maxdepth 1 -name 'nx-plugin-delivery-proof.*' | wc -l | tr -d ' ')"
expect_eq "sandbox removed on a failed run too" "0" "$leftfail"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
