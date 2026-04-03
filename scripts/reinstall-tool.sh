#!/bin/bash
# Reinstall the nx CLI tool while preserving any optional extras
# (e.g., [mineru], [local]) from the previous installation.
#
# Usage: scripts/reinstall-tool.sh [source]
#   source: install source (default: "." for local dev, use "conexus" for PyPI)

set -euo pipefail

SOURCE="${1:-.}"
RECEIPT="$(uv tool dir)/conexus/uv-receipt.toml"

# Extract extras from the uv receipt if it exists
EXTRAS=""
if [[ -f "$RECEIPT" ]]; then
    # Parse extras = ["mineru", "local"] from TOML
    EXTRAS=$(python3 -c "
import re, sys
text = open('$RECEIPT').read()
m = re.search(r'extras\s*=\s*\[([^\]]*)\]', text)
if m:
    extras = re.findall(r'\"([^\"]+)\"', m.group(1))
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

# Symlink extra entrypoints that uv tool doesn't expose automatically.
# uv only symlinks the package's own console_scripts (nx, nx-mcp);
# dependency console_scripts (mineru-api, mineru, etc.) stay hidden
# in the tool venv bin dir.
TOOL_BIN="$(uv tool dir)/conexus/bin"
LOCAL_BIN="${HOME}/.local/bin"

if [[ "$EXTRAS" == *"mineru"* && -d "$TOOL_BIN" ]]; then
    for cmd in mineru-api mineru; do
        if [[ -f "$TOOL_BIN/$cmd" ]]; then
            ln -sf "$TOOL_BIN/$cmd" "$LOCAL_BIN/$cmd"
            echo "Symlinked: $cmd"
        fi
    done
fi
