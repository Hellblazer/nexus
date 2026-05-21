# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 stress harness.

Deterministic stress scenarios that gate phase transitions in the
storage substrate split. Each phase ships a section of the harness
covering its new code paths; merge of a phase PR gates on the
relevant ``tests/stress/test_*_stress.py`` file passing in CI plus
24h of shakedown on ``main`` (RDR-120 §Approach validation regime,
2026-05-21 amendment).

Run locally with::

    uv run pytest -m stress tests/stress/

Scenarios are explicit pass/fail tests, not passive observation. They
cover the daemon-mode failure modes that the prior calendar-soak
regime relied on operators happening to surface:

- Concurrency storms (parallel clients hitting the same backend)
- Connection churn (rapid open/close cycles; socket cleanup)
- Daemon crash + supervisor respawn
- Spawn-lock contention (two parallel starts)
- Malformed input (oversized frames, garbage bytes, partial frames)
- Slow / dead clients (hung sends, mid-transmission disconnects)
- Process suspend / resume (SIGSTOP / SIGCONT sleep-wake analogue)
- Memory profile (insert + delete cycles; bounded growth)
- Discovery file lifecycle (stale after reboot; manual corruption)
- HttpClient / HttpServer timeout invariants
"""
