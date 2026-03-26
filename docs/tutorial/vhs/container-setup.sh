#!/usr/bin/env bash
# Container Setup for Tutorial Recording
#
# Run this INSIDE the Claude Box container before recording sections 5-7.
# Idempotent — safe to re-run (resets T2 memory and demo repo).
#
# Prerequisites:
#   - Claude Box container running
#   - uv, git, node already available (Claude Box provides these)
#
# Usage:
#   ./container-setup.sh
#
# After this script completes:
#   1. cd ~/demo-repo
#   2. tmux new-session -s tutorial
#   3. Run: claude    (and log in — the ONE manual step)
#   4. In a second terminal: ./05-nexus-in-claude.sh
#
# NOTE: bd (beads) is not installed. RDR commands will show
# "Beads not available" — this is expected and handled gracefully.

set -euo pipefail

echo "=== Tutorial Container Setup ==="

# --- Step 1: Install nexus ---
echo "[1/6] Installing nexus..."
uv tool install conexus --force
nx --version

# --- Step 2: Reset T2 state (idempotent for re-runs) ---
echo "[2/6] Resetting T2 memory..."
nx memory delete --project demo-repo --all -y 2>/dev/null || true

# --- Step 3: Create demo repo ---
echo "[3/6] Creating demo repo..."
rm -rf ~/demo-repo
mkdir -p ~/demo-repo
(
cd ~/demo-repo
git init

cat > auth.py << 'PYEOF'
"""Authentication middleware using JWT tokens."""

import jwt
from datetime import datetime, timedelta

SECRET_KEY = "demo-secret-key"
TOKEN_EXPIRY_HOURS = 24

def create_token(user_id: str) -> str:
    """Create a JWT token with 24-hour expiry."""
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def verify_token(token: str) -> dict:
    """Verify and decode a JWT token."""
    return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])

def auth_middleware(request):
    """Middleware that validates JWT tokens on incoming requests."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise PermissionError("No authentication token provided")
    return verify_token(token)
PYEOF

cat > retry.py << 'PYEOF'
"""Retry logic with exponential backoff."""

import time
import random

def retry_with_backoff(fn, max_attempts=3, base_delay=1.0):
    """Call fn with exponential backoff on failure."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            time.sleep(delay)
PYEOF

cat > database.py << 'PYEOF'
"""Database connection pool management."""

MAX_CONNECTIONS = 10

class ConnectionPool:
    """Simple connection pool with configurable max connections."""

    def __init__(self, dsn: str, max_conn: int = MAX_CONNECTIONS):
        self.dsn = dsn
        self.max_conn = max_conn
        self._connections = []

    def acquire(self):
        """Acquire a connection from the pool."""
        if len(self._connections) >= self.max_conn:
            raise RuntimeError("Connection pool exhausted")
        conn = self._connect()
        self._connections.append(conn)
        return conn

    def release(self, conn):
        """Return a connection to the pool."""
        self._connections.remove(conn)

    def _connect(self):
        """Create a new database connection."""
        return {"dsn": self.dsn, "active": True}
PYEOF

# error_handler.py — deliberately left with bare except/pass for review demo
# Committed separately so git diff shows it as a change in section 6
cat > error_handler.py << 'PYEOF'
"""Centralized error handling."""

def parse(data):
    """Parse data from file content."""
    return {"raw": data, "parsed": True}
PYEOF

cat > docs/getting-started.md << 'MDEOF'
# Getting Started

Install dependencies and run the project.

## Quick Start

1. Clone the repo
2. Install dependencies
3. Run the tests
MDEOF

mkdir -p docs
cat > docs/architecture.md << 'MDEOF'
# Architecture

## Components

- **auth.py** — JWT authentication middleware
- **retry.py** — Retry logic with exponential backoff
- **database.py** — Connection pool management
- **error_handler.py** — Centralized error handling
MDEOF

cat > README.md << 'MDEOF'
# Demo Repo

A small demo project for the nexus tutorial.

## Architecture

- `auth.py` — JWT authentication middleware
- `retry.py` — Retry logic with exponential backoff
- `database.py` — Connection pool management
- `error_handler.py` — Centralized error handling
MDEOF

# Commit the clean files first
git add auth.py retry.py database.py README.md docs/
git commit -m "Initial demo repo for tutorial"

# Now add the bad error_handler.py as an uncommitted change
# (section 6 review demo will find it via git diff)
git add error_handler.py
git commit -m "Add error handler"

# Add the bare except/pass version as an uncommitted change
cat > error_handler.py << 'PYEOF'
"""Centralized error handling."""

def process_file(path):
    try:
        data = open(path).read()
        return parse(data)
    except:
        pass

def parse(data):
    """Parse data from file content."""
    return {"raw": data, "parsed": True}
PYEOF

# Create docs/rdr/ directory stub for section 7
mkdir -p docs/rdr

) # end subshell

# --- Step 4: Index the repo ---
echo "[4/6] Indexing demo repo..."
cd ~/demo-repo
nx index repo .

# --- Step 5: Populate memory ---
echo "[5/6] Populating memory..."
nx memory put "Auth uses JWT tokens with 24-hour expiry" \
    --project demo-repo --title auth-notes --ttl permanent

nx memory put "Connection pooling with max 10 connections for database layer" \
    --project demo-repo --title db-config --ttl permanent

# --- Step 6: Verify ---
echo "[6/6] Verifying..."
nx doctor
echo ""

echo "=== Setup Complete ==="
echo ""
echo "Demo repo: ~/demo-repo (indexed, memory populated)"
echo "  - error_handler.py has uncommitted bare except/pass (for review demo)"
echo "  - docs/rdr/ directory exists (for RDR demo)"
echo "  - bd is NOT installed (RDR commands show 'Beads not available' — expected)"
echo "nx version: $(nx --version)"
echo ""
echo "Next steps:"
echo "  1. cd ~/demo-repo"
echo "  2. tmux new-session -s tutorial"
echo "  3. claude   (log in — the one manual step)"
echo "  4. In another terminal: ./05-nexus-in-claude.sh"
