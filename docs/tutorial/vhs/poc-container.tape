# POC: Full soup-to-nuts install + usage in a clean container
#
# Prerequisites:
#   docker build -t nexus-tutorial docs/tutorial/vhs/
#   docker run -d --name nexus-tutorial nexus-tutorial sleep 3600
#
# This tape runs VHS on the host, with the shell inside the container.

Output "poc-container.mp4"

Set FontSize 16
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Shell "bash"

# Drop into the container
Type "docker exec -it nexus-tutorial bash"
Enter
Sleep 2s

# --- Step 1: Install uv ---
Type "curl -LsSf https://astral.sh/uv/install.sh | sh"
Enter
Sleep 10s

# Source the path so uv is available
Type "source $HOME/.local/bin/env"
Enter
Sleep 2s

Type "uv --version"
Enter
Sleep 3s

# --- Step 2: Install nexus ---
Type "uv tool install conexus"
Enter
Sleep 20s

Type "nx --version"
Enter
Sleep 4s

# --- Step 3: Health check (local mode, no API keys needed) ---
Type "NX_LOCAL=1 nx doctor"
Enter
Sleep 8s

# --- Step 4: Store a memory ---
Type@60ms 'nx memory put "Auth uses JWT tokens with 24h expiry" --project demo --title auth-notes --ttl permanent'
Enter
Sleep 5s

# --- Step 5: Search memory ---
Type 'nx memory search "JWT"'
Enter
Sleep 5s

# --- Step 6: Index the demo repo ---
Type "NX_LOCAL=1 nx index repo ."
Enter
Sleep 30s

# --- Step 7: Semantic search ---
Type 'NX_LOCAL=1 nx search "how does authentication work"'
Enter
Sleep 8s

Type 'NX_LOCAL=1 nx search "error handling" --corpus code'
Enter
Sleep 8s

# Done
Sleep 3s
