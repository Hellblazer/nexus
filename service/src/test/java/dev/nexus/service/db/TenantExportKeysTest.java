// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantExport.Column;
import dev.nexus.service.db.TenantExport.ExportTable;
import dev.nexus.service.db.TenantExportKeys.MalformedKeyException;
import org.junit.jupiter.api.Test;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * The export key cursor (RDR-248 gate S4d, bead nexus-csayy.2): one encoding, typed binds from the registry's
 * column types, and a text form for every key type that reads back what the binary COPY row holds.
 */
class TenantExportKeysTest {

    private static final List<Column> CHUNK_KEY = TenantExport.exportable("chunks").orElseThrow().primaryKey();

    @Test
    void everyExportableTablesKeyTypeIsSupported() {
        int examined = 0;
        for (ExportTable t : TenantExport.TABLES) {
            if (!t.exported()) continue;
            for (Column c : t.primaryKey()) {
                assertThat(TenantExportKeys.SUPPORTED_TYPES).as(t.name() + "." + c.name()).contains(c.type());
                assertThat(TenantExportKeys.dataType(c.type())).isNotNull();
                examined++;
            }
        }
        assertThat(examined).as("primary-key columns examined").isGreaterThan(30);
    }

    @Test
    void chunksCompositeKeyRoundTripsAndBindsTypedValues() {
        byte[] chash = HexFormat.of().parseHex("00ff" + "ab".repeat(30));
        List<String> parts = List.of("t-1", "knowledge__x__bge__v1", HexFormat.of().formatHex(chash), "bge-base-en-v15-768");
        String cursor = TenantExportKeys.encode(parts);

        assertThat(cursor).matches("[A-Za-z0-9_-]+");
        assertThat(new String(Base64.getUrlDecoder().decode(cursor), StandardCharsets.UTF_8))
            .isEqualTo("[\"t-1\",\"knowledge__x__bge__v1\",\"" + HexFormat.of().formatHex(chash) + "\",\"bge-base-en-v15-768\"]");
        List<String> back = TenantExportKeys.decode(cursor, CHUNK_KEY);
        assertThat(back).isEqualTo(parts);
        List<Object> binds = TenantExportKeys.binds(CHUNK_KEY, back);
        assertThat(binds.get(0)).isEqualTo("t-1");
        assertThat((byte[]) binds.get(2)).isEqualTo(chash);
    }

    @Test
    void textWithQuotesBackslashesAndUnicodeSurvivesTheCursor() {
        List<Column> key = List.of(new Column("tenant_id", "text"), new Column("key", "text"));
        String nasty = "it's a \\ \"key\" é中😀 ; DROP TABLE x;--";
        String cursor = TenantExportKeys.encode(List.of("t", nasty));
        assertThat(TenantExportKeys.binds(key, TenantExportKeys.decode(cursor, key)).get(1)).isEqualTo(nasty);
    }

    @Test
    void bindsTypeEveryKeyTypeAndRefuseAnyOtherSpelling() {
        assertThat(TenantExportKeys.binds(List.of(new Column("id", "bigint")), List.of("-9223372036854775808")))
            .containsExactly(Long.MIN_VALUE);
        assertThat(TenantExportKeys.binds(List.of(new Column("p", "integer")), List.of("7"))).containsExactly(7);
        UUID uuid = UUID.fromString("123e4567-e89b-12d3-a456-426614174000");
        assertThat(TenantExportKeys.binds(List.of(new Column("j", "uuid")), List.of(uuid.toString()))).containsExactly(uuid);
        assertThat(TenantExportKeys.binds(List.of(new Column("ts", "timestamp with time zone")),
            List.of("2026-11-12T10:15:30.123456Z")))
            .containsExactly(OffsetDateTime.ofInstant(Instant.parse("2026-11-12T10:15:30.123456Z"), ZoneOffset.UTC));

        // one number has one text, so a cursor a client builds by hand and the engine's own agree
        for (String bad : List.of("+1", "01", "-0", "1.0", " 1", "", "0x10", "9223372036854775808")) {
            assertThatThrownBy(() -> TenantExportKeys.binds(List.of(new Column("id", "bigint")), List.of(bad)))
                .as("bigint %s", bad).isInstanceOf(MalformedKeyException.class);
        }
        for (String bad : List.of("ABCD", "abc", "zz", "0x00")) {
            assertThatThrownBy(() -> TenantExportKeys.binds(List.of(new Column("c", "bytea")), List.of(bad)))
                .as("bytea %s", bad).isInstanceOf(MalformedKeyException.class);
        }
        for (String bad : List.of("123E4567-E89B-12D3-A456-426614174000", "1-1-1-1-1", "nope")) {
            assertThatThrownBy(() -> TenantExportKeys.binds(List.of(new Column("j", "uuid")), List.of(bad)))
                .as("uuid %s", bad).isInstanceOf(MalformedKeyException.class);
        }
        for (String bad : List.of("2026-11-12T10:15:30+01:00", "2026-11-12", "yesterday")) {
            assertThatThrownBy(() -> TenantExportKeys.binds(List.of(new Column("ts", "timestamp with time zone")), List.of(bad)))
                .as("timestamptz %s", bad).isInstanceOf(MalformedKeyException.class);
        }
        assertThatThrownBy(() -> TenantExportKeys.binds(List.of(new Column("k", "text")), List.of("a\0b")))
            .isInstanceOf(MalformedKeyException.class);
    }

    @Test
    void aCursorOfTheWrongShapeIsMalformed() {
        String threeOfFour = TenantExportKeys.encode(List.of("t", "c", "00"));
        assertThatThrownBy(() -> TenantExportKeys.decode(threeOfFour, CHUNK_KEY))
            .isInstanceOf(MalformedKeyException.class).hasMessageContaining("3 elements").hasMessageContaining("4");
        assertThatThrownBy(() -> TenantExportKeys.decode("***not base64***", CHUNK_KEY))
            .isInstanceOf(MalformedKeyException.class);
        String notJson = Base64.getUrlEncoder().withoutPadding().encodeToString("{not json".getBytes(StandardCharsets.UTF_8));
        assertThatThrownBy(() -> TenantExportKeys.decode(notJson, CHUNK_KEY)).isInstanceOf(MalformedKeyException.class);
        String object = Base64.getUrlEncoder().withoutPadding().encodeToString("{\"a\":1}".getBytes(StandardCharsets.UTF_8));
        assertThatThrownBy(() -> TenantExportKeys.decode(object, CHUNK_KEY)).isInstanceOf(MalformedKeyException.class);
        String numbers = Base64.getUrlEncoder().withoutPadding().encodeToString("[1,2,3,4]".getBytes(StandardCharsets.UTF_8));
        assertThatThrownBy(() -> TenantExportKeys.decode(numbers, CHUNK_KEY))
            .isInstanceOf(MalformedKeyException.class).hasMessageContaining("not a string");
        assertThatThrownBy(() -> TenantExportKeys.decode("", CHUNK_KEY)).isInstanceOf(MalformedKeyException.class);
    }

    @Test
    void renderReadsBackWhatTheBinaryRowHolds() {
        assertThat(TenantExportKeys.render(new Column("k", "text"), "héllo".getBytes(StandardCharsets.UTF_8)))
            .isEqualTo("héllo");
        assertThat(TenantExportKeys.render(new Column("id", "bigint"), ByteBuffer.allocate(8).putLong(-42L).array()))
            .isEqualTo("-42");
        assertThat(TenantExportKeys.render(new Column("p", "integer"), ByteBuffer.allocate(4).putInt(9).array()))
            .isEqualTo("9");
        assertThat(TenantExportKeys.render(new Column("c", "bytea"), new byte[]{0, (byte) 0xff, 0x10})).isEqualTo("00ff10");
        UUID u = UUID.fromString("123e4567-e89b-12d3-a456-426614174000");
        assertThat(TenantExportKeys.render(new Column("j", "uuid"),
            ByteBuffer.allocate(16).putLong(u.getMostSignificantBits()).putLong(u.getLeastSignificantBits()).array()))
            .isEqualTo(u.toString());
        // a binary timestamptz is microseconds since 2000-01-01T00:00:00Z
        Instant at = Instant.parse("2026-11-12T10:15:30.123456Z");
        long micros = (at.getEpochSecond() - 946_684_800L) * 1_000_000L + at.getNano() / 1_000L;
        assertThat(TenantExportKeys.render(new Column("ts", "timestamp with time zone"),
            ByteBuffer.allocate(8).putLong(micros).array())).isEqualTo("2026-11-12T10:15:30.123456Z");
        // before 2000 and a whole second, to pin the floor division
        Instant before = Instant.parse("1969-07-20T20:17:40Z");
        long beforeMicros = (before.getEpochSecond() - 946_684_800L) * 1_000_000L;
        assertThat(TenantExportKeys.render(new Column("ts", "timestamp with time zone"),
            ByteBuffer.allocate(8).putLong(beforeMicros).array())).isEqualTo("1969-07-20T20:17:40Z");
        assertThatThrownBy(() -> TenantExportKeys.render(new Column("id", "bigint"), new byte[3]))
            .isInstanceOf(IllegalStateException.class);
    }

    @Test
    void renderedTextParsesBackToTheSameBind() {
        Instant at = Instant.parse("2026-11-12T10:15:30.000100Z");
        long micros = (at.getEpochSecond() - 946_684_800L) * 1_000_000L + 100;
        Column ts = new Column("ts", "timestamp with time zone");
        String text = TenantExportKeys.render(ts, ByteBuffer.allocate(8).putLong(micros).array());
        assertThat(TenantExportKeys.binds(List.of(ts), List.of(text)))
            .containsExactly(OffsetDateTime.ofInstant(at, ZoneOffset.UTC));
    }
}
