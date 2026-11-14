// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/** The header's schema marker (RDR-248, bead nexus-csayy.2): a number when the journal was read, JSON null when not. */
class TenantExportHandlerTest {

    @Test
    void aSchemaChangesetCountThatCouldNotBeReadIsEmittedAsJsonNull() throws Exception {
        ObjectNode unread = new ObjectMapper().createObjectNode();
        TenantExportHandler.putSchemaChangesetCount(unread, null);
        assertThat(unread.has("schema_changeset_count")).as("present, never omitted").isTrue();
        assertThat(unread.get("schema_changeset_count").isNull()).isTrue();
        assertThat(new ObjectMapper().writeValueAsString(unread)).isEqualTo("{\"schema_changeset_count\":null}");

        ObjectNode read = new ObjectMapper().createObjectNode();
        TenantExportHandler.putSchemaChangesetCount(read, 64L);
        assertThat(read.get("schema_changeset_count").asLong()).isEqualTo(64L);
    }
}
