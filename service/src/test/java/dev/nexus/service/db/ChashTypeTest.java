/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Chash — the parse-don't-validate content-address newtype (nexus-z4skl).
 * The factory is the sole enforcement point for the 32-lowercase-hex
 * canonical form; these tests pin every rejection message a boundary 400
 * relies on (especially the self-diagnosing 64-char hint).
 */
class ChashTypeTest {

    private static final String VALID = "0123456789abcdef0123456789abcdef";

    @Test
    void fromHex_roundTrips_canonicalForm() {
        Chash c = Chash.fromHex(VALID);
        assertThat(c.toHex()).isEqualTo(VALID);
        assertThat(c.toString()).isEqualTo(VALID);
    }

    @Test
    void fromHex_rejects_full64_withLengthAndHint() {
        String full = VALID + VALID;
        assertThatThrownBy(() -> Chash.fromHex(full))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("got 64 chars")
            .hasMessageContaining("full sha256")
            .hasMessageContaining("fromSha256Hex");
    }

    @Test
    void fromHex_rejects_otherLengths_withActualLength() {
        assertThatThrownBy(() -> Chash.fromHex("abc"))
            .hasMessageContaining("got 3 chars");
        assertThatThrownBy(() -> Chash.fromHex(""))
            .hasMessageContaining("got 0 chars");
        assertThatThrownBy(() -> Chash.fromHex(null))
            .hasMessageContaining("got null");
    }

    @Test
    void fromHex_rejects_uppercase_andNonHex_withIndex() {
        assertThatThrownBy(() -> Chash.fromHex("0123456789ABCDEF0123456789abcdef"))
            .hasMessageContaining("LOWERCASE")
            .hasMessageContaining("index 10");
        assertThatThrownBy(() -> Chash.fromHex("z123456789abcdef0123456789abcdef"))
            .hasMessageContaining("'z' at index 0");
    }

    @Test
    void fromSha256Hex_isTheNamedTruncationHome() {
        String full = "aabbccddeeff00112233445566778899" + "ffeeddccbbaa99887766554433221100";
        assertThat(Chash.fromSha256Hex(full).toHex())
            .isEqualTo("aabbccddeeff00112233445566778899");
        assertThatThrownBy(() -> Chash.fromSha256Hex(VALID))
            .hasMessageContaining("FULL 64-char")
            .hasMessageContaining("32 chars");
    }

    @Test
    void ofText_matchesSha256Prefix() throws Exception {
        String text = "the amaranthine zeppelin quotient";
        var md = java.security.MessageDigest.getInstance("SHA-256");
        String fullHex = java.util.HexFormat.of().formatHex(
            md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        assertThat(Chash.ofText(text).toHex()).isEqualTo(fullHex.substring(0, 32));
        // and agrees with the named truncation factory
        assertThat(Chash.ofText(text)).isEqualTo(Chash.fromSha256Hex(fullHex));
    }

    @Test
    void requireCanonical_prefixesLabel() {
        assertThatThrownBy(() -> Chash.requireCanonical("a".repeat(64), "rows[3].chash"))
            .hasMessageStartingWith("rows[3].chash: ")
            .hasMessageContaining("got 64 chars");
        assertThat(Chash.requireCanonical(VALID, "x")).isEqualTo(VALID);
    }

    @Test
    void requireLength32_lengthOnly_allowsNonHex() {
        // The vector/chash_index seams' contract is the DB CHECK (length=32),
        // NOT sha256-hex — the serving contract test upserts non-hex ids.
        String nonHex = "p4a-c100000000000000000000000000";
        assertThat(nonHex).hasSize(32);
        assertThat(Chash.requireLength32(nonHex, "ids[0]")).isEqualTo(nonHex);
        assertThatThrownBy(() -> Chash.requireLength32("a".repeat(64), "ids[1]"))
            .hasMessageStartingWith("ids[1]: ")
            .hasMessageContaining("got 64 chars")
            .hasMessageContaining("full sha256");
        assertThatThrownBy(() -> Chash.requireLength32(null, "ids[2]"))
            .hasMessageContaining("got null");
    }

    @Test
    void equality_isOverContents() {
        assertThat(Chash.fromHex(VALID)).isEqualTo(Chash.fromHex(VALID));
        assertThat(Chash.fromHex(VALID).hashCode()).isEqualTo(Chash.fromHex(VALID).hashCode());
        assertThat(Chash.fromHex(VALID))
            .isNotEqualTo(Chash.fromHex("ffffffffffffffffffffffffffffffff"));
    }
}
