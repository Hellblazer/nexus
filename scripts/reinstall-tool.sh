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
