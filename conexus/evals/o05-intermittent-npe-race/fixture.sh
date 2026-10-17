#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Scaffold for this case (nexus-dg7i6.7): builds the project the prompt talks
# about in the current, empty directory. Fixed content and fixed commit dates,
# no network, so the outcome graders read the same files on every run.
set -euo pipefail
export GIT_AUTHOR_DATE=2026-10-01T12:00:00Z GIT_COMMITTER_DATE=2026-10-01T12:00:00Z
git init -q
git checkout -q -b main
git config user.name aurora
git config user.email aurora@example.invalid
mkdir -p src
cat > README.md <<'MD'
# aurora

A small stream-processing service.

Run: python -m src.cli --help
MD
cat > CLAUDE.md <<'MD'
# aurora

Aurora is a small stream-processing service. Code is in src/.
MD
cat > src/__init__.py <<'PY'
PY
cat > src/ingest.py <<'PY'
"""Batch ingest for aurora."""
import time


class FixedClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def now(self) -> float:
        return self.t


def batch(events, size, clock=time):
    out, cur = [], []
    for e in events:
        cur.append((clock.now() if hasattr(clock, "now") else clock.time(), e))
        if len(cur) == size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out
PY
cat > src/cli.py <<'PY'
"""Command line entry point for aurora."""
import argparse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="aurora")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--clock", choices=["wall", "fixed"], default="wall")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    print(args)


if __name__ == "__main__":
    main()
PY
git add -A
git commit -qm "Add ingest batching with an injected clock"
mkdir -p src/main/java/aurora
cat > src/main/java/aurora/ConnectionPool.java <<'JAVA'
package aurora;

/** Fixed-size pool of connections shared by the nightly job's worker threads. */
public final class ConnectionPool {
    private final Connection[] slots;
    private int next = 0;

    public ConnectionPool(int size) {
        slots = new Connection[size];
        for (int i = 0; i < size; i++) {
            slots[i] = new Connection(i);
        }
    }

    /** Takes the next free connection. */
    public Connection borrow() {
        if (next >= slots.length) {
            next = 0;
        }
        Connection c = slots[next];
        slots[next] = null;
        next++;
        return c;
    }

    /** Puts a connection back in the first empty slot. */
    public void giveBack(Connection c) {
        for (int i = 0; i < slots.length; i++) {
            if (slots[i] == null) {
                slots[i] = c;
                return;
            }
        }
    }
}
JAVA
cat > src/main/java/aurora/Connection.java <<'JAVA'
package aurora;

public final class Connection {
    private final int id;

    Connection(int id) {
        this.id = id;
    }

    public void execute(String sql) {
        // talks to the warehouse
    }
}
JAVA
cat > src/main/java/aurora/NightlyJob.java <<'JAVA'
package aurora;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/** Rolls up the day's events, eight partitions at a time. */
public final class NightlyJob {
    public static void main(String[] args) throws InterruptedException {
        ConnectionPool pool = new ConnectionPool(8);
        ExecutorService workers = Executors.newFixedThreadPool(8);
        for (int p = 0; p < 64; p++) {
            final int partition = p;
            workers.submit(() -> {
                Connection conn = pool.borrow();
                try {
                    conn.execute("INSERT INTO rollup SELECT * FROM events WHERE part = " + partition);
                } finally {
                    pool.giveBack(conn);
                }
            });
        }
        workers.shutdown();
        workers.awaitTermination(1, TimeUnit.HOURS);
    }
}
JAVA
git add -A
git commit -qm "Nightly rollup job with a shared connection pool"
python3 - <<'PY'
p = "src/main/java/aurora/NightlyJob.java"
s = open(p).read()
s = s.replace("                Connection conn = pool.borrow();\n",
              "                Connection conn = pool.borrow();\n"
              "                if (conn == null) {\n"
              "                    conn = pool.borrow();\n"
              "                }\n")
open(p, "w").write(s)
PY
git commit -qam "Retry borrow once when the pool hands back null"
sed -i.bak 's/new ConnectionPool(8)/new ConnectionPool(16)/' src/main/java/aurora/NightlyJob.java && rm -f src/main/java/aurora/NightlyJob.java.bak
git commit -qam "Double the pool size"
