// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f4wcg: the Voyage request body is a BYTE contract, not a semantic one.
 *
 * <p>Voyage serves per-request stable results that can differ across
 * byte-different-but-semantically-equal bodies by ~4e-5 cosine (region
 * dependent) — exactly the class of drift the embed-parity gate hunts.
 * The Java body must therefore be byte-identical to what the production
 * Python path (voyageai SDK 0.3.7 via chromadb VoyageAIEmbeddingFunction)
 * puts on the wire: insertion key order, explicit JSON nulls for the unset
 * params, python-json.dumps {@code ", "}/{@code ": "} separators, and
 * ensure_ascii escaping.
 *
 * <p>Expected strings below are CAPTURED python output
 * ({@code json.dumps({...})} with the SDK's exact param dict), not
 * hand-written. Regenerate with the snippet in the parity test docs if the
 * SDK's wire format ever changes.
 */
class VoyageEmbedderBodyTest {

    private final VoyageEmbedder embedder = new VoyageEmbedder("test-key", "voyage-code-3", null);

    @Test
    void bodyIsByteIdenticalToPythonSdkWireFormat() {
        assertThat(embedder.buildJson(List.of("The quick brown fox jumps over the lazy dog.")))
                .isEqualTo("{\"input\": [\"The quick brown fox jumps over the lazy dog.\"], "
                        + "\"model\": \"voyage-code-3\", \"input_type\": null, \"truncation\": true, "
                        + "\"output_dtype\": null, \"output_dimension\": null, \"encoding_format\": \"base64\"}");
    }

    @Test
    void nonAsciiAndEscapesMatchPythonEnsureAscii() {
        // python: json.dumps(["héllo ☃ ..."], ...) — non-ASCII as backslash-u
        // escapes, quotes/backslash/tab escaped identically.
        assertThat(embedder.buildJson(List.of("héllo ☃ \"q\" \\ tab\there", "second")))
                .isEqualTo("{\"input\": [\"h\\u00e9llo \\u2603 \\\"q\\\" \\\\ tab\\there\", \"second\"], "
                        + "\"model\": \"voyage-code-3\", \"input_type\": null, \"truncation\": true, "
                        + "\"output_dtype\": null, \"output_dimension\": null, \"encoding_format\": \"base64\"}");
    }

    @Test
    void jsonStringEscapingBoundariesMatchPython() {
        // Captured python: json.dumps('\x7e\x7f\x80 \x01\x1f \b\f\n\r\t \\ "')
        assertThat(VoyageEmbedder.jsonString("~\u007F\u0080 \u0001\u001F \b\f\n\r\t \\ \""))
                .isEqualTo("\"~\\u007f\\u0080 \\u0001\\u001f \\b\\f\\n\\r\\t \\\\ \\\"\"");
        // Astral char: python emits a lowercase surrogate-pair escape.
        assertThat(VoyageEmbedder.jsonString("😀")).isEqualTo("\"\\ud83d\\ude00\"");
    }
}
