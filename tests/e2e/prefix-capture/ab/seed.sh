#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-ly9c2: runs INSIDE the A/B capture container, against the engine that
# container's `nx init --no-autostart` provisioned, before the container is
# `docker commit`ed as nexus-ab-seeded:2.1.300. Never run it on a host.
set -euo pipefail
nx memory put "Root cause of the flaky test_ingest_batch test: the fixture read wall-clock time, so batches straddling a second boundary were split differently. Fixed by injecting a FixedClock into the ingest fixture (bead AUR-112, 2026-09-30)." -p aurora -t flaky-ingest-test-root-cause --tags testing,flaky
nx memory put "Connection pool sizing for the ingest worker: chose a pool size of 24 after measuring p99 query latency at 18 ms with 24 connections versus 41 ms with 12. Above 24 the database CPU saturated. Decision by the storage team, 2026-10-02." -p aurora -t db-pool-sizing-decision --tags database,decision
nx memory put "Release checklist for aurora: run the soak test for 2 hours, check the schema migration dry run, bump the version in three manifests, tag, and announce in the release channel." -p aurora -t release-checklist --tags release
nx store put - -c stream-processing -t watermark-handling-notes --tags streaming <<'T'
Watermark handling in the aurora stream processor. The watermark advances to the minimum event time seen across partitions minus a fixed bound. Events that arrive after the watermark are still accepted if they are within the allowed lateness of 90 seconds; they update the open window and emit a correction. Events later than 90 seconds past the watermark go to the late-events side output and are counted in the late_dropped metric. Idle partitions are excluded from the minimum after 30 seconds of silence so they cannot stall the watermark.
T
nx store put - -c stream-processing -t backpressure-design --tags streaming <<'T'
Backpressure design for aurora. Operators use credit-based flow control: each downstream task grants its upstream a window of 512 credits, one credit per buffer. When credits run out the upstream stops sending and its own input buffers fill, which propagates the pressure to the source. The source then pauses its Kafka consumer instead of buffering in memory. We rejected a rate-limiter design because it reacted too slowly to sudden skew.
T
nx store put - -c stream-processing -t exactly-once-sinks --tags streaming <<'T'
Exactly-once sinks in aurora. Transactional sinks use two-phase commit tied to checkpoints: data is pre-committed when a checkpoint barrier arrives and committed when the checkpoint completes. Sinks that cannot do transactions must be idempotent, keyed by event id plus partition offset, so replay after recovery overwrites rather than duplicates. A sink that is neither transactional nor idempotent only gets at-least-once.
T
nx store put - -c observability -t metrics-naming-convention --tags metrics <<'T'
Metric naming convention for aurora: snake_case, unit suffix (_seconds, _bytes, _total), and the component as a prefix, for example ingest_batch_duration_seconds. Labels never carry unbounded values such as user ids.
T
nx memory list -p aurora
nx search "allowed lateness watermark" --corpus knowledge -n 3
