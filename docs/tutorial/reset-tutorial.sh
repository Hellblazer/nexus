#!/bin/bash
set -euo pipefail

# reset-tutorial.sh — Reset everything to a clean state for recording
#
# Run this before each recording session (or between takes).
# Idempotent: safe to run multiple times.

echo "=== Nexus Tutorial Reset ==="

# 1. Clean nexus state (forces local-only mode — no API keys visible on camera)
echo "Cleaning nexus state..."
rm -rf ~/.config/nexus ~/.local/share/nexus
# Temporarily hide cloud env vars so nx doctor shows local mode
unset VOYAGE_API_KEY CHROMA_API_KEY CHROMA_TENANT CHROMA_DATABASE 2>/dev/null
echo "  Nexus state cleaned (cloud env vars unset for this session)."

# 2. Reset demo-repo to initial commit
DEMO_REPO="$HOME/demo-repo"
if [ -d "$DEMO_REPO/.git" ]; then
    echo "Resetting demo-repo to initial commit..."
    cd "$DEMO_REPO"
    git checkout main 2>/dev/null || true
    git reset --hard HEAD
    git clean -fd
    rm -f tasks.db  # remove any SQLite DB created by running the app
    echo "  demo-repo reset."
else
    echo "  WARNING: $DEMO_REPO not found. Create it first (see recording-guide.md)."
fi

# 3. Verify tools are installed
echo "Verifying tools..."
nx --version 2>/dev/null || echo "  WARNING: nx not found. Run: uv tool install conexus"
python3 --version 2>/dev/null || echo "  WARNING: python3 not found"
git --version 2>/dev/null || echo "  WARNING: git not found"

# 4. Run nx doctor
echo ""
echo "nx doctor:"
nx doctor 2>/dev/null || echo "  nx doctor failed — check installation"

# 5. Source the shell setup
echo ""
echo "=== Ready ==="
echo "Now run: source ~/.nexus-tutorial-rc"
echo "Then open Terminal with the 'Nexus Tutorial' profile."
