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
#      under the scrubbed one with no inherited CLAUDE_CONFIG_DIR / ANTHROPIC_API_KEY,
#      git and python ignore the real HOME's config (poisoned canaries), and no
#      sandbox is left behind;
#   f. the registry must read PREV after the install and carry an installPath
#      (none is guessed); a stale installPath, a deleted file left in the cache, a
#      failed install and a failed update of an uninstalled plugin all fail;
#   g. with no --source the real GitHub marketplace is added in a second scrubbed
#      HOME and every plugin's version and ref must be NEW; with --source the verdict
#      says it was not checked.
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

[ -f "$PROOF" ] || { bad "tests/e2e/plugin-delivery-proof.sh does not exist"; echo "plugin_delivery_proof_test.sh: $PASS passed, $FAIL failed"; exit 1; }

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
# Mode (file next to this script): ok | oldfiles (registry moves, files stay) | nomove (update is a no-op)
#   | keepdeleted (new files arrive, a file deleted upstream stays) | stalepath (version moves, installPath
#   stays on the old dir) | noinstallpath (registry entries carry no installPath) | installwrong (install
#   registers another version) | installskip (install does nothing and exits 0).
D="$(cd "$(dirname "$0")" && pwd)"
MODE="$(cat "$D/mode" 2>/dev/null || echo ok)"
echo "HOME=$HOME ARGV=$*" >>"$D/calls.log"
# Report only WHETHER these were inherited, never a value.
echo "cfgdir=[${CLAUDE_CONFIG_DIR:+seen}] apikey=[${ANTHROPIC_API_KEY:+seen}]" >>"$D/env.log"
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
register() {  # <plugin@market> <version> <installPath or ->
  /usr/bin/python3 - "$REG" "$1" "$2" "$3" <<'PY'
import json, os, sys
f, key, ver, path = sys.argv[1:]
d = json.load(open(f)) if os.path.exists(f) else {"version": 2, "plugins": {}}
e = {"scope": "user", "version": ver}
if path != "-":
    e["installPath"] = path
d["plugins"][key] = [e]
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
ipath() { if [ "$MODE" = "noinstallpath" ]; then echo "-"; else echo "$1"; fi; }
case "$1 ${2:-}" in
  "marketplace add")
    case "$3" in
      /*)  # a local directory marketplace
        printf '%s' "$3" >"$ST/market_path"
        /usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$3/.claude-plugin/marketplace.json" >"$ST/market_name" ;;
      *)  # a GitHub shorthand: the "real" marketplace is whatever real_market.json holds
        [ -f "$D/real_market.json" ] || { echo "stub: no real marketplace" >&2; exit 1; }
        name="$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$D/real_market.json")"
        mkdir -p "$P/marketplaces/$name/.claude-plugin"
        cp "$D/real_market.json" "$P/marketplaces/$name/.claude-plugin/marketplace.json"
        printf '%s' "$name" >"$ST/market_name" ;;
    esac
    echo "Added marketplace $(cat "$ST/market_name")"; exit 0 ;;
  "marketplace update") echo "Updated marketplace $3"; exit 0 ;;
esac
case "$1" in
  install)
    plug="${2%@*}"; mk="${2#*@}"
    [ "$MODE" = "installskip" ] && { echo "Installed $2"; exit 0; }
    ver="$(entry "$plug" | cut -d'|' -f4)"
    fetch_into "$plug" "$mk" "$P/cache/$mk/$plug/$ver" || exit 1
    [ "$MODE" = "installwrong" ] && ver="9.9.9"
    register "$2" "$ver" "$(ipath "$P/cache/$mk/$plug/$ver")"; echo "Installed $2"; exit 0 ;;
  update)
    plug="${2%@*}"; mk="${2#*@}"
    cur="$(current "$2")"
    [ -n "$cur" ] || { echo "Plugin $2 is not installed" >&2; exit 1; }   # the real CLI refuses
    ver="$(entry "$plug" | cut -d'|' -f4)"
    if [ "$MODE" = "nomove" ] || [ "$cur" = "$ver" ]; then echo "$plug is already at the latest version ($cur)"; exit 0; fi
    dest="$P/cache/$mk/$plug/$ver"
    if [ "$MODE" = "oldfiles" ]; then
      mkdir -p "$dest"; cp -R "$P/cache/$mk/$plug/$cur/." "$dest/"
    else
      fetch_into "$plug" "$mk" "$dest" || exit 1
      [ "$MODE" = "keepdeleted" ] && cp -R -n "$P/cache/$mk/$plug/$cur/." "$dest/"
    fi
    reg_path="$dest"; [ "$MODE" = "stalepath" ] && reg_path="$P/cache/$mk/$plug/$cur"
    register "$2" "$ver" "$(ipath "$reg_path")"; echo "Updated $plug from $cur to $ver. Restart to apply"; exit 0 ;;
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
  printf '%s' "$mode" >"$SHIMS/mode"; : >"$SHIMS/calls.log"; : >"$SHIMS/env.log"
  local out rc
  out="$(PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" "$@" 2>&1)"; rc=$?
  printf '%s\nrc=%s\n' "$out" "$rc"
}

# The marketplace the stub serves for a GitHub shorthand (`marketplace add owner/name`).
set_real() {  # <tag> | none
  if [ "$1" = "none" ]; then rm -f "$SHIMS/real_market.json"
  else gitq "$SRC" show "$1:.claude-plugin/marketplace.json" >"$SHIMS/real_market.json"; fi
}
set_real v1.0.1
# A proof run with NO --source: the script fetches https://github.com/fixture/repo.git, which an
# env-supplied url rewrite (not any config file) points at the local fixture.
run_live() {  # run_live <mode> <args...>
  local mode="$1"; shift
  printf '%s' "$mode" >"$SHIMS/mode"; : >"$SHIMS/calls.log"; : >"$SHIMS/env.log"
  local out rc
  out="$(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="url.file://$SRC.insteadOf" GIT_CONFIG_VALUE_0="https://github.com/fixture/repo.git" \
    PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" --repo fixture/repo "$@" 2>&1)"; rc=$?
  printf '%s\nrc=%s\n' "$out" "$rc"
}

# ── a. the update moves the files ────────────────────────────────────────────
echo "Test a: a normal update that delivers passes, with witnesses and the claude version"
out="$(run_proof ok --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "PASSED line" "$out" "PLUGIN DELIVERY PROOF PASSED -- 1.0.0 -> 1.0.1, 7 witness file(s) (4 non-manifest), claude 9.9.9-stub;"
expect_has "says the GitHub marketplace was not checked" "$out" "GitHub marketplace not checked (--source given)"
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

# ── f. registry and cache checks ─────────────────────────────────────────────
echo "Test f1: new files arrive but a file deleted upstream stays in the cache: FAILED, naming it"
out="$(run_proof keepdeleted --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names the surviving file" "$out" "beta/old.md: deleted in v1.0.1 but still in the plugin cache"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"

echo "Test f2: the registry says NEW but installPath points at a stale dir: FAILED"
out="$(run_proof stalepath --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names a stale file" "$out" "alpha/skills/a.md"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"

echo "Test f3: a registry entry with no installPath is a FAILURE, no path is guessed"
out="$(run_proof noinstallpath --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "says no installPath" "$out" "has no installPath"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"

echo "Test f4: the registry must read PREV right after the install, before any update"
out="$(run_proof installwrong --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names the install step and the version" "$out" "after the install step and before any update"
expect_has "names the wrong version" "$out" "'9.9.9', want 1.0.0"
expect_lacks "the update never ran" "$(cat "$SHIMS/calls.log")" "plugin update"
out="$(run_proof installskip --prev 1.0.0 --new 1.0.1 --source "$SRC")"
expect_has "an install that registers nothing: FAILED" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "no registry at all" "$out" "install left no installed_plugins.json"

echo "Test f5: the stub refuses to update a plugin that was never installed, like the real CLI"
mkdir -p "$WORKDIR/stubhome"
printf 'ok' >"$SHIMS/mode"
stub_rc=0; HOME="$WORKDIR/stubhome" "$SHIMS/claude" plugin update nobody@nowhere >/dev/null 2>&1 || stub_rc=$?
expect_eq "stub update of an uninstalled plugin exits 1" "1" "$stub_rc"

# ── g. the real GitHub marketplace ───────────────────────────────────────────
echo "Test g1: without --source the real marketplace is added in a second scrubbed HOME and must say NEW"
set_real v1.0.1
out="$(run_live ok --prev 1.0.0 --new 1.0.1)"
expect_has "PASSED line" "$out" "PLUGIN DELIVERY PROOF PASSED -- 1.0.0 -> 1.0.1"
expect_has "says the marketplace was checked" "$out" "GitHub marketplace fixture/repo checked (3 plugin(s) at 1.0.1, ref v1.0.1)"
expect_lacks "not the not-checked note" "$out" "not checked"
calls="$(cat "$SHIMS/calls.log")"
expect_has "the real marketplace add ran" "$calls" "plugin marketplace add fixture/repo"
live_homes="$(sed -n 's/^HOME=\([^ ]*\) ARGV=plugin marketplace add fixture\/repo$/\1/p' "$SHIMS/calls.log")"
case "$live_homes" in "$WORKDIR"/*) ok "the real marketplace was added under a scratch HOME" ;; *) bad "live HOME not scratch: [$live_homes]" ;; esac
local_home="$(sed -n 's/^HOME=\([^ ]*\) ARGV=plugin install alpha.*/\1/p' "$SHIMS/calls.log")"
local_home="${local_home%%$'\n'*}"
if [ -n "$live_homes" ] && [ "$live_homes" != "$local_home" ]; then ok "the real marketplace has its own HOME"; else bad "live HOME [$live_homes] equals the install HOME [$local_home]"; fi

echo "Test g2: a real marketplace still at the old release FAILS"
set_real v1.0.0
out="$(run_live ok --prev 1.0.0 --new 1.0.1)"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names the real marketplace" "$out" "the real GitHub marketplace fixture/repo does not list the new release"
expect_has "names the stale version" "$out" "version '1.0.0'"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"

echo "Test g3: a real marketplace that cannot be added is UNVERIFIED, not a pass"
set_real none
out="$(run_live ok --prev 1.0.0 --new 1.0.1)"
expect_has "UNVERIFIED line" "$out" "PLUGIN DELIVERY PROOF UNVERIFIED"
expect_has "exit 2" "$out" "rc=2"

echo "Test g4: a real marketplace that lists no plugin FAILS"
set_real v1.0.1
/usr/bin/python3 -c 'import json,sys;p=sys.argv[1];m=json.load(open(p));m["plugins"]=[];json.dump(m,open(p,"w"))' "$SHIMS/real_market.json"
out="$(run_live ok --prev 1.0.0 --new 1.0.1)"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "says it lists none" "$out" "the real marketplace lists no plugin"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"

echo "Test g5: a real marketplace that dropped a shipped plugin FAILS"
set_real v1.0.1
/usr/bin/python3 -c 'import json,sys;p=sys.argv[1];m=json.load(open(p));m["plugins"]=m["plugins"][1:];json.dump(m,open(p,"w"))' "$SHIMS/real_market.json"
out="$(run_live ok --prev 1.0.0 --new 1.0.1)"
expect_has "FAILED line" "$out" "PLUGIN DELIVERY PROOF FAILED"
expect_has "names the dropped plugin" "$out" "but is not listed"
expect_lacks "no PASSED" "$out" "PLUGIN DELIVERY PROOF PASSED"
set_real v1.0.1

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
echo "do-not-touch" >"$CANARY/.claude/settings.json"
before="$(cd "$CANARY" && find . -type f -exec cksum {} + | sort)"
before_tree="$(cd "$CANARY" && find . | sort)"
# Poisoned canaries: each would break the run if the script let it in. A real HOME's git config
# (global and XDG) that rewrites file:// to nowhere, an inherited GIT_CONFIG_GLOBAL doing the same,
# and a PYTHONPATH json module that records a hit. claude's own config dir and API key are exported
# so the stub can report whether it inherited them.
mkdir -p "$CANARY/.config/git" "$WORKDIR/pypoison"
printf '[url "file:///nonexistent-canary/"]\n\tinsteadOf = file://\n' >"$CANARY/.gitconfig"
cp "$CANARY/.gitconfig" "$CANARY/.config/git/config"
cp "$CANARY/.gitconfig" "$WORKDIR/poisoned-global-gitconfig"
# GIT_CONFIG_GLOBAL replaces the global config files but NOT the default excludes file, which
# lives under the HOME: this one makes the marketplace commit empty if the script's HOME is the real one.
printf 'marketplace.json\n' >"$CANARY/.config/git/ignore"
printf 'open("%s", "a").write("hit\\n")\nraise ImportError("poisoned canary")\n' "$WORKDIR/py-canary-hit" >"$WORKDIR/pypoison/json.py"
: >"$WORKDIR/py-canary-hit"
before="$(cd "$CANARY" && find . -type f -exec cksum {} + | sort)"
before_tree="$(cd "$CANARY" && find . | sort)"
touch "$WORKDIR/marker"; sleep 1
printf 'ok' >"$SHIMS/mode"; : >"$SHIMS/calls.log"; : >"$SHIMS/env.log"
out="$(HOME="$CANARY" GIT_CONFIG_GLOBAL="$WORKDIR/poisoned-global-gitconfig" PYTHONPATH="$WORKDIR/pypoison" \
  CLAUDE_CONFIG_DIR="$CANARY/claude-cfg" ANTHROPIC_API_KEY="canary-not-a-key" \
  PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" --prev 1.0.0 --new 1.0.1 --source "$SRC" 2>&1; echo "rc=$?")"
expect_has "still passes with a populated, poisoned real HOME" "$out" "PLUGIN DELIVERY PROOF PASSED"
after="$(cd "$CANARY" && find . -type f -exec cksum {} + | sort)"
after_tree="$(cd "$CANARY" && find . | sort)"
newer="$(find "$CANARY" -newer "$WORKDIR/marker" | wc -l | tr -d ' ')"
expect_eq "real HOME file contents unchanged" "$before" "$after"
expect_eq "real HOME tree unchanged (nothing created)" "$before_tree" "$after_tree"
expect_eq "nothing in the real HOME modified or touched during the run" "0" "$newer"
homes="$(sed -n 's/^HOME=\([^ ]*\) ARGV=.*/\1/p' "$SHIMS/calls.log" | sort -u)"
expect_lacks "claude never saw the real HOME" "$homes" "$CANARY"
case "$homes" in "$WORKDIR"/*) ok "claude ran under a scratch HOME inside TMPDIR" ;; *) bad "claude HOME not under TMPDIR: [$homes]" ;; esac
envlog="$(cat "$SHIMS/env.log")"
expect_has "claude ran at all (the env log is not vacuous)" "$envlog" "cfgdir=["
expect_lacks "claude inherited no CLAUDE_CONFIG_DIR" "$envlog" "cfgdir=[seen]"
expect_lacks "claude inherited no ANTHROPIC_API_KEY" "$envlog" "apikey=[seen]"
expect_lacks "the key value never reached the output" "$out" "canary-not-a-key"
# Positive control: the stub does report an inherited variable, so the two assertions above can fail.
: >"$SHIMS/env.log"
CLAUDE_CONFIG_DIR=x ANTHROPIC_API_KEY=y HOME="$WORKDIR/stubhome" "$SHIMS/claude" --version >/dev/null
expect_has "control: the stub sees an inherited config dir" "$(cat "$SHIMS/env.log")" "cfgdir=[seen] apikey=[seen]"
pyhits="$(grep -c . "$WORKDIR/py-canary-hit")" || pyhits=0
expect_eq "no python read PYTHONPATH (the harness runs isolated)" "0" "$pyhits"
n_calls="$(grep -c . "$SHIMS/calls.log")"
n_scrubbed="$(grep -c "^HOME=$WORKDIR/" "$SHIMS/calls.log")"
expect_eq "every claude call used the scrubbed HOME" "$n_calls" "$n_scrubbed"
left="$(find "$WORKDIR" -maxdepth 1 -name 'nx-plugin-delivery-proof.*' | wc -l | tr -d ' ')"
expect_eq "sandbox removed on exit" "0" "$left"
leftfail="$(printf 'oldfiles' >"$SHIMS/mode"; PATH="$SHIMS:$TOOLS" TMPDIR="$WORKDIR" bash "$PROOF" --prev 1.0.0 --new 1.0.1 --source "$SRC" >/dev/null 2>&1; find "$WORKDIR" -maxdepth 1 -name 'nx-plugin-delivery-proof.*' | wc -l | tr -d ' ')"
expect_eq "sandbox removed on a failed run too" "0" "$leftfail"

echo
echo "plugin_delivery_proof_test.sh: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
