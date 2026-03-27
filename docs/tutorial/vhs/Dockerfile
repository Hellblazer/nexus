FROM python:3.12-slim

# Minimal deps — intentionally no uv, no nx
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Clean prompt, no noise
ENV PS1="$ " \
    TERM=xterm-256color \
    PYTHONDONTWRITEBYTECODE=1

# Demo repo for indexing
RUN git config --global user.email "demo@example.com" && \
    git config --global user.name "Demo User" && \
    mkdir -p /demo-repo/src /demo-repo/docs && \
    cd /demo-repo && git init && \
    printf 'def authenticate(user, password):\n    """Verify credentials against the user store."""\n    token = create_jwt(user, expiry_hours=24)\n    return token\n' > src/auth.py && \
    printf 'class RetryHandler:\n    """Exponential backoff with jitter."""\n    def __init__(self, max_retries=3, base_delay=1.0):\n        self.max_retries = max_retries\n        self.base_delay = base_delay\n\n    def execute(self, fn):\n        for attempt in range(self.max_retries):\n            try:\n                return fn()\n            except Exception:\n                delay = self.base_delay * (2 ** attempt)\n                time.sleep(delay)\n        raise RuntimeError("max retries exceeded")\n' > src/retry.py && \
    printf '# Getting Started\n\nInstall with pip, then run the server.\n\n## Authentication\n\nUses JWT tokens with 24-hour expiry.\n' > docs/getting-started.md && \
    printf '# Error Handling\n\nAll errors use structured logging via structlog.\nRetry logic uses exponential backoff.\n' > docs/error-handling.md && \
    git add -A && git commit -m "initial commit"

# Clean prompt — must be in .bashrc, ENV PS1 gets overwritten by login shell
RUN echo 'export PS1="$ "' >> /root/.bashrc

WORKDIR /demo-repo
CMD ["bash"]
