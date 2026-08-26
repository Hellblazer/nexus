#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Live-holder flip ladder against the REAL conexus artifact. nexus-utpuw.17.

Driven by ``tests/e2e/gen-flip-live-holder.sh``, which owns the sandbox, the
env scrub and the PASSED sentinel. This half owns the ladder.

WHY PYTHON RATHER THAN MORE SHELL. The holder must stay alive ACROSS builds and
flips while the ladder keeps talking to it, which in bash means a coprocess and
a long-lived two-way pipe — the exact shape project memory records deadlocking
under Bash 5.3 when macOS degrades pipes, and the reason bead .16 says to prefer
sibling .py files. Orchestrating from Python keeps one process holding both ends
and removes the hazard rather than working around it.

STDLIB ONLY, and deliberately: this drives the artifact from OUTSIDE. Importing
nexus here would resolve through the dev checkout and quietly test that instead
of the generation under the shim.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORK = Path(os.environ["NX_GATE_WORK"])
REPO = Path(os.environ["NX_GATE_REPO"])
TOOLS = Path(os.environ["NX_TOOLS_DIR"])
BIN = Path(os.environ["NX_BIN_DIR"])
INSTALL = REPO / "src" / "nexus" / "_install"

#: Import-failure signatures, taken from the project's own vocabulary in
#: ``src/nexus/mcp/_stale_host.py`` rather than invented here. A tool call that
#: fails for want of a BACKEND is expected in this sandbox; one that fails for
#: want of a MODULE is nexus-q3xrx.
IMPORT_MARKERS = (
    "ImportError", "ModuleNotFoundError", "cannot import name",
    "No module named", "AttributeError", "has no attribute",
)


def fail(message: str) -> "None":
    print(f"GEN-FLIP LIVE-HOLDER FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def step(message: str) -> None:
    print(f"  → {message}", flush=True)


def sh(snippet: str, *, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, timeout=timeout
    )


def build_generation(label: str) -> Path:
    """One REAL conexus generation, built from this checkout."""
    started = time.time()
    done = subprocess.run(
        ["bash", str(INSTALL / "install_generation.sh"), "--source", str(REPO)],
        capture_output=True, text=True, timeout=1800,
    )
    if done.returncode != 0:
        fail(f"generation {label} did not build:\n{done.stderr[-3000:]}")
    generation = Path(done.stdout.strip().splitlines()[-1])
    step(f"generation {label} built in {time.time() - started:.0f}s: {generation.name}")
    return generation


def flip_and_shim(generation: Path) -> None:
    done = sh(
        f'. "{INSTALL}/flip.sh"; . "{INSTALL}/shims.sh"; '
        f'nx_flip_current "{generation}" "{TOOLS}" && '
        f'nx_write_shims "{generation}" "{BIN}"'
    )
    if done.returncode != 0:
        fail(f"flip/shim failed for {generation.name}:\n{done.stderr[-2000:]}")


def holders_of(generation: Path) -> list[int]:
    """The REAL census — the same function GC and `nx doctor` consult."""
    done = sh(f'. "{INSTALL}/census.sh"; nx_generation_holder_pids "{generation}"')
    if done.returncode != 0:
        fail(f"census failed for {generation.name}:\n{done.stderr[-2000:]}")
    return [int(t) for t in done.stdout.split()]


class Holder:
    """A real nx-mcp process, spawned THROUGH the shim, driven over real MCP."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.proc = subprocess.Popen(
            [str(BIN / "nx-mcp")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._next_id = 0
        self._handshake()

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float = 120.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def _handshake(self) -> None:
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "gen-flip-gate", "version": "1"}},
        })
        if self._read() is None:
            stderr = self.proc.stderr.read()[-2000:] if self.proc.stderr else ""
            fail(f"holder {self.label} never completed MCP initialize:\n{stderr}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict) -> str:
        """A REAL tools/call. Returns the response text."""
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        response = self._read()
        if response is None:
            fail(f"holder {self.label} did not answer tools/call {tool}")
        blocks = response.get("result", {}).get("content", [])
        return "\n".join(b.get("text", "") for b in blocks)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=30)


def fabricate_retirement_generation(stamp: str) -> Path:
    """A generation-shaped directory with a valid receipt, and nothing else.

    GC reads DIRECTORY SHAPE and a receipt, never an interpreter, so the two
    generations whose only job is to take over `current` and `previous` need
    not be built. The two the holder actually binds to ARE real builds — the
    cost belongs where the subject is. Stamps sort last on purpose: keep-N
    depends on lexical order being creation order.
    """
    generation = TOOLS / f"gen-{stamp}"
    (generation / "bin").mkdir(parents=True)
    (generation / "nexus-install.json").write_text(json.dumps({
        "schema": 1, "installer_schema": 1, "version": "0.0.0",
        "spec": "conexus", "source_kind": "registry", "source": "conexus",
        "python": "3.12", "base_interpreter": "/nonexistent",
        "created_at": "2026-08-26T00:00:00Z", "extras": [],
    }, indent=2) + "\n")
    return generation


def assert_no_import_failure(where: str, text: str) -> None:
    hit = [m for m in IMPORT_MARKERS if m in text]
    if hit:
        fail(
            f"{where}: the tool call failed for want of a MODULE {hit} — that is "
            f"nexus-q3xrx, not an absent backend:\n{text[:1500]}"
        )


def main() -> int:
    print("gen-flip live-holder ladder (real conexus generations)")

    # A1 — generation A, flipped, a REAL nx-mcp holder spawned through the shim.
    gen_a = build_generation("A")
    flip_and_shim(gen_a)
    holder = Holder("A")
    step(f"holder pid {holder.pid} spawned through {BIN / 'nx-mcp'}")

    try:
        # A real MCP tools/call, which is what forces the deferred imports a
        # bare handshake does not. In this scrubbed sandbox it is EXPECTED to
        # report an absent backend; what it must never report is a missing
        # module.
        before = holder.call("collection_list", {})
        assert_no_import_failure("before the flip", before)
        if not before.strip():
            fail("the pre-flip tool call returned nothing, so the comparison "
                 "below would be vacuous")

        # HERMETICITY IS ASSERTED, NOT ASSUMED. Measured while building this
        # gate: an nx-mcp started with an ambient environment answered a real
        # `search` call out of the OPERATOR'S live collections. In this sealed
        # sandbox there is no backend, so the only correct answer is a named
        # error — and if this call ever SUCCEEDS, the seal has broken and the
        # gate is reading someone's real data. That must be loud, never a
        # quietly greener-looking pass.
        if not before.lstrip().startswith("Error:"):
            fail(
                "the sandbox is not hermetic — collection_list SUCCEEDED where "
                "no backend should exist, so this gate may be observing real "
                f"data rather than the sandbox:\n{before[:800]}"
            )
        step(f"pre-flip tools/call answered: {before.splitlines()[0][:96]}")

        # A2 — the census, which is how nexus itself attributes holders.
        if holder.pid not in holders_of(gen_a):
            fail(f"the census does not see pid {holder.pid} holding {gen_a.name}; "
                 "GC would consider that tree free while a real MCP host runs "
                 "from it")
        step("census attributes the holder to generation A")

        # A3 — generation B, flipped underneath the running holder.
        gen_b = build_generation("B")
        if gen_b == gen_a:
            fail("the second build reused the first generation directory")
        flip_and_shim(gen_b)
        step("current flipped to generation B")

        # A4/A5 — the SAME live process must still serve, and must still be
        # attributed to A. This is the artifact-level statement of the property:
        # q3xrx's concrete symptom was exactly this call failing after an
        # upgrade (95 cacert tracebacks).
        after = holder.call("collection_list", {})
        assert_no_import_failure("after the flip", after)
        if after != before:
            fail("the running holder answered DIFFERENTLY after the flip, so "
                 f"the flip reached into it:\nbefore: {before[:600]}\n"
                 f"after:  {after[:600]}")
        step(f"post-flip tools/call answered identically: "
             f"{after.splitlines()[0][:96]}")

        if holder.pid not in holders_of(gen_a):
            fail("after the flip the census no longer attributes the holder to "
                 "generation A, so GC could reap the tree it is executing")
        if holder.pid in holders_of(gen_b):
            fail("the census attributes the live holder to generation B, which "
                 "it has never executed")
        step("census still attributes the holder to generation A, not B")

        # A6 — a NEW spawn through the same shim gets the NEW generation.
        fresh = Holder("B")
        try:
            if fresh.pid not in holders_of(gen_b):
                fail("a freshly spawned holder was not attributed to generation "
                     "B, so the shim is not resolving the pointer per spawn")
            step("a fresh spawn lands in generation B")
        finally:
            fresh.stop()

        # A7 — GC must not reap a generation with a live holder.
        #
        # THE SETUP IS THE TEST (measured on .16): with only A and B, the flip
        # makes A `previous`, so A survives never-delete rule (b) whatever rule
        # (c) does — and with A retired but nothing else reapable, "A survived"
        # cannot be told from "GC did nothing". Two fabricated generations take
        # over `current` and `previous`, which retires A from both AND leaves B
        # genuinely reapable now that its holder has exited.
        for stamp in ("99999998T000000Z", "99999999T000000Z"):
            retirement = fabricate_retirement_generation(stamp)
            done = sh(f'. "{INSTALL}/flip.sh"; '
                      f'nx_flip_current "{retirement}" "{TOOLS}"')
            if done.returncode != 0:
                fail(f"could not flip to {retirement.name}:\n{done.stderr[-1500:]}")

        done = sh(f'. "{INSTALL}/gc.sh"; nx_gc_generations --keep 1 "{TOOLS}"')
        output = done.stdout + done.stderr
        if not gen_a.is_dir():
            fail(f"GC reaped generation A out from under a live nx-mcp holder — "
                 f"never-delete rule (c):\n{output}")
        if gen_b.exists():
            fail("GC reaped nothing reapable, so generation A's survival is "
                 f"indistinguishable from an inert GC pass:\n{output}")
        step("GC kept the held generation A and reaped the unheld generation B")

        # The holder must still be ALIVE at the end. A holder that died half way
        # would have made several assertions above pass for the wrong reason.
        if holder.proc.poll() is not None:
            fail(f"the holder exited during the ladder (rc="
                 f"{holder.proc.returncode}); assertions above cannot be trusted")
        step("holder still alive at the end of the ladder")
    finally:
        holder.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
