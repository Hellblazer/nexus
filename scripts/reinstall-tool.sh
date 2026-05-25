#!/bin/bash
# Reinstall the nx CLI tool, preserving optional extras (e.g. [local])
# from the previous installation.
#
# nexus-2fyb: mineru was promoted from extras to a default dep. The
# previous "preserve extras" logic silently propagated empty-extras
# state for any install that didn't start with [mineru] — which was
# every fresh install per README. mineru is now always present;
# only genuinely-optional extras like [local] are receipt-driven.
#
# Usage: scripts/reinstall-tool.sh [source]
#   source: install source (default: "." for local dev, use "conexus" for PyPI)

set -euo pipefail

SOURCE="${1:-.}"
RECEIPT="$(uv tool dir)/conexus/uv-receipt.toml"

EXTRAS=""
if [[ -f "$RECEIPT" ]]; then
    # nexus-2fyb code-review R5-I1: pass the receipt path via an env var
    # rather than shell-interpolating it into the python -c heredoc. The
    # prior `open('$RECEIPT')` form was vulnerable to Python-injection if
    # $RECEIPT ever contained a quote (low real-world risk via uv tool
    # dir, but a clean fix).
    EXTRAS=$(NEXUS_RECEIPT_PATH="$RECEIPT" python3 -c "
import os, re
text = open(os.environ['NEXUS_RECEIPT_PATH']).read()
m = re.search(r'extras\s*=\s*\[([^\]]*)\]', text, re.DOTALL)
if m:
    extras = re.findall(r'\"([^\"]+)\"', m.group(1))
    # 'mineru' is now a default dep — drop it if a stale receipt still lists it
    extras = [e for e in extras if e != 'mineru']
    if extras:
        print(','.join(extras))
" 2>/dev/null || true)
fi

if [[ -n "$EXTRAS" ]]; then
    echo "Preserving extras: [$EXTRAS]"
    uv tool install --reinstall --from "${SOURCE}[${EXTRAS}]" conexus
else
    uv tool install --reinstall "$SOURCE"
fi

nx --version

# Symlink dependency console_scripts (mineru-api, mineru) into ~/.local/bin.
# uv only auto-symlinks the project's own entrypoints (nx, nx-mcp); deps stay
# inside the tool venv. mineru is always present now (nexus-2fyb), so
# unconditionally symlink it if the binaries exist.
TOOL_BIN="$(uv tool dir)/conexus/bin"
LOCAL_BIN="${HOME}/.local/bin"

if [[ -d "$TOOL_BIN" ]]; then
    for cmd in mineru-api mineru; do
        if [[ -f "$TOOL_BIN/$cmd" ]]; then
            ln -sf "$TOOL_BIN/$cmd" "$LOCAL_BIN/$cmd"
            echo "Symlinked: $cmd"
        fi
    done
fi

# nexus-5ldk1: a running T2 daemon froze its code at start and now predates
# this reinstall. Bring it to the freshly-installed version so the reinstall
# is live, not pending a manual `nx daemon t2 stop && ensure-running`.
# ensure-running is version-aware: no-op on a current daemon, graceful cycle
# on a stale one. Best-effort; never fails the reinstall.
if command -v nx >/dev/null 2>&1; then
    nx daemon t2 ensure-running --quiet --timeout=10 2>/dev/null || \
        echo "(note: daemon cycle skipped/failed; run 'nx daemon t2 ensure-running' manually)"
fi
