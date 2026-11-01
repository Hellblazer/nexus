// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * A test-only tuple template with the shape the retired {@code ledger/<session_id>} had (bead
 * nexus-onjvw): {@code id_from: keys}, two pinned keys one of which carries an allowed-value set,
 * optional string dimensions (one with an allowed-value set), {@code take.enabled: false}, a zero
 * {@code max_body_bytes} and a 90-day retention.
 *
 * <p>No bundled template has that combination any more, and a good part of the tuple repository's
 * contract is exercised only by it: the {@code id_from: keys} refire clamp, a pinned key's value
 * set, the dimension checks, the keys-only reply-target refusal, the read-only
 * ({@code TakeDisabled}) path and the zero body cap. It is loaded as a second registry source
 * ({@code TemplateRegistry.loadAtBoot(dir, ...)}), the way {@code TupleRepositoryTest}'s
 * {@code probe/<id>} is, so a production template file is never bent to a test's need.
 */
public final class ReadOnlyProbeTemplate {

    /** The subspace prefix, e.g. {@code "probe-ro/" + sessionId}. */
    public static final String PREFIX = "probe-ro/";

    /** The template's registry name. */
    public static final String NAME = "probe-ro/<session_id>";

    private static final String YAML = """
            name: probe-ro/<session_id>

            keys:
              agent_id:
              kind:
                values: [start, report]

            dimensions:
              agent_type:
                type: string
              commit:
                type: string
              t2_ref:
                type: string
              verify:
                type: string
                values: [present, absent]

            id_from: keys

            take:
              enabled: false

            retention_seconds: 7776000

            max_body_bytes: 0
            """;

    private ReadOnlyProbeTemplate() {
    }

    /** Writes {@code probe-ro.yaml} into {@code dir} and returns {@code dir}. */
    public static Path writeTo(Path dir) throws IOException {
        Files.writeString(dir.resolve("probe-ro.yaml"), YAML, StandardCharsets.UTF_8);
        return dir;
    }
}
