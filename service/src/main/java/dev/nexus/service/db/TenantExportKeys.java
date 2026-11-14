// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantExport.Column;
import org.jooq.DataType;
import org.jooq.impl.SQLDataType;

import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.UUID;

/**
 * The export page route's key cursor (RDR-248, bead nexus-csayy.2): the encoding of {@code after} and
 * {@code X-Nexus-Export-Last-Key}, the typed binds a table's primary-key column types give a parsed
 * {@code after}, and the text form of a key field read back out of a binary {@code COPY} row.
 *
 * <p><b>Encoding.</b> A key is one text per primary-key column, in the registry's column order (for
 * {@code chunks}: {@code tenant_id, collection, chash, embedding_model}). The texts form a JSON array of
 * strings, and the array's UTF-8 bytes are written as unpadded base64url. So the cursor is opaque, one
 * token, safe in a URL and in a header whatever a key holds, and a client hands back the last key it was
 * given without looking inside it. Text per type:
 * <ul>
 *   <li>{@code text}: the value itself;</li>
 *   <li>{@code bigint}, {@code integer}: decimal, no plus sign, no leading zero other than "0" itself;</li>
 *   <li>{@code bytea}: lowercase hex, two digits a byte, no prefix ({@code chash} is 64 characters);</li>
 *   <li>{@code uuid}: the canonical 8-4-4-4-12 lowercase form;</li>
 *   <li>{@code timestamp with time zone}: an ISO-8601 instant in UTC with a trailing {@code Z}, up to
 *       microseconds ({@code 2026-11-12T10:15:30.123456Z}).</li>
 * </ul>
 * A cursor with the wrong number of elements, a non-string element, a text that does not parse as its
 * column's type, or invalid base64 or JSON is malformed ({@link MalformedKeyException}); the route answers
 * 400. The keyset predicate is a row-value comparison over the whole primary key, so the order is the
 * primary key's own order for each column's type and collation, which is the order the index serves.
 */
public final class TenantExportKeys {

    private TenantExportKeys() {
    }

    /** {@code after} does not decode to a key of the table's shape. */
    public static final class MalformedKeyException extends IllegalArgumentException {
        public MalformedKeyException(String message) {
            super(message);
        }
    }

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** Microseconds from the Unix epoch to 2000-01-01T00:00:00Z, the origin of a binary timestamp. */
    private static final long PG_EPOCH_OFFSET_MICROS = 946_684_800L * 1_000_000L;

    /** The primary-key column types the registry holds; {@link #dataType} refuses any other. */
    static final List<String> SUPPORTED_TYPES =
        List.of("text", "bigint", "integer", "bytea", "uuid", "timestamp with time zone");

    /** The jOOQ type a key column's bind takes. */
    static DataType<?> dataType(String pgType) {
        return switch (pgType) {
            case "text" -> SQLDataType.VARCHAR;
            case "bigint" -> SQLDataType.BIGINT;
            case "integer" -> SQLDataType.INTEGER;
            case "bytea" -> SQLDataType.BLOB;
            case "uuid" -> SQLDataType.UUID;
            case "timestamp with time zone" -> SQLDataType.TIMESTAMPWITHTIMEZONE;
            default -> throw new IllegalStateException("export key type not supported: " + pgType);
        };
    }

    /** The cursor for {@code parts} (one text per key column). */
    public static String encode(List<String> parts) {
        try {
            return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(MAPPER.writeValueAsBytes(parts));
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("a list of strings did not serialize", e);
        }
    }

    /** The texts in {@code after}, one per column of {@code key}; throws {@link MalformedKeyException}. */
    public static List<String> decode(String after, List<Column> key) {
        byte[] json;
        try {
            json = Base64.getUrlDecoder().decode(after);
        } catch (IllegalArgumentException e) {
            throw new MalformedKeyException("after is not base64url");
        }
        JsonNode node;
        try {
            node = MAPPER.readTree(json);
        } catch (java.io.IOException e) {
            throw new MalformedKeyException("after does not hold a JSON array");
        }
        if (node == null || !node.isArray()) {
            throw new MalformedKeyException("after does not hold a JSON array");
        }
        if (node.size() != key.size()) {
            throw new MalformedKeyException("after has " + node.size() + " elements; this table's key has "
                + key.size() + " (" + names(key) + ")");
        }
        List<String> parts = new ArrayList<>(key.size());
        for (int i = 0; i < key.size(); i++) {
            JsonNode element = node.get(i);
            if (!element.isTextual()) {
                throw new MalformedKeyException("after element " + i + " (" + key.get(i).name()
                    + ") is not a string");
            }
            parts.add(element.asText());
        }
        return parts;
    }

    /** The typed values for {@code parts}, as {@code key}'s column types read them. */
    public static List<Object> binds(List<Column> key, List<String> parts) {
        List<Object> values = new ArrayList<>(key.size());
        for (int i = 0; i < key.size(); i++) {
            values.add(parse(key.get(i), parts.get(i)));
        }
        return values;
    }

    private static Object parse(Column column, String text) {
        try {
            return switch (column.type()) {
                case "text" -> {
                    if (text.indexOf('\0') >= 0) {
                        throw new MalformedKeyException(column.name() + " holds a NUL character");
                    }
                    yield text;
                }
                case "bigint" -> Long.parseLong(canonicalInteger(text));
                case "integer" -> Integer.parseInt(canonicalInteger(text));
                case "bytea" -> {
                    if (text.length() % 2 != 0) {
                        throw new MalformedKeyException(column.name() + " is not whole bytes of hex");
                    }
                    if (!text.equals(text.toLowerCase(java.util.Locale.ROOT))) {
                        throw new MalformedKeyException(column.name() + " is not lowercase hex");
                    }
                    yield HexFormat.of().parseHex(text);
                }
                case "uuid" -> {
                    UUID uuid = UUID.fromString(text);
                    if (!uuid.toString().equals(text)) {
                        throw new MalformedKeyException(column.name() + " is not a canonical lowercase uuid");
                    }
                    yield uuid;
                }
                case "timestamp with time zone" -> {
                    if (!text.endsWith("Z")) {
                        throw new MalformedKeyException(column.name() + " is not a UTC instant ending in Z");
                    }
                    yield OffsetDateTime.ofInstant(Instant.parse(text), ZoneOffset.UTC);
                }
                default -> throw new IllegalStateException("export key type not supported: " + column.type());
            };
        } catch (NumberFormatException | DateTimeParseException e) {
            throw new MalformedKeyException(column.name() + " does not parse as " + column.type());
        } catch (IllegalArgumentException e) {
            if (e instanceof MalformedKeyException m) throw m;
            throw new MalformedKeyException(column.name() + " does not parse as " + column.type());
        }
    }

    /** Rejects a plus sign and a leading zero, so one number has one text and a cursor round-trips exactly. */
    private static String canonicalInteger(String text) {
        if (text.isEmpty() || text.startsWith("+")
                || (text.length() > 1 && text.startsWith("0"))
                || (text.length() > 2 && text.startsWith("-0"))
                || "-0".equals(text)) {
            throw new NumberFormatException(text);
        }
        return text;
    }

    /** The text form of one key field of a binary {@code COPY} row, by the column's type. */
    static String render(Column column, byte[] field) {
        ByteBuffer buf = ByteBuffer.wrap(field);
        switch (column.type()) {
            case "text":
                try {
                    return StandardCharsets.UTF_8.newDecoder()
                        .onMalformedInput(CodingErrorAction.REPORT)
                        .onUnmappableCharacter(CodingErrorAction.REPORT)
                        .decode(buf).toString();
                } catch (CharacterCodingException e) {
                    throw new IllegalStateException("a text key field is not UTF-8", e);
                }
            case "bigint":
                requireWidth(column, field, 8);
                return Long.toString(buf.getLong());
            case "integer":
                requireWidth(column, field, 4);
                return Integer.toString(buf.getInt());
            case "bytea":
                return HexFormat.of().formatHex(field);
            case "uuid":
                requireWidth(column, field, 16);
                return new UUID(buf.getLong(), buf.getLong()).toString();
            case "timestamp with time zone":
                requireWidth(column, field, 8);
                long micros = buf.getLong() + PG_EPOCH_OFFSET_MICROS;
                return Instant.ofEpochSecond(Math.floorDiv(micros, 1_000_000L),
                    Math.floorMod(micros, 1_000_000L) * 1_000L).toString();
            default:
                throw new IllegalStateException("export key type not supported: " + column.type());
        }
    }

    private static void requireWidth(Column column, byte[] field, int width) {
        if (field.length != width) {
            throw new IllegalStateException(column.name() + " (" + column.type() + ") is " + field.length
                + " bytes in the binary row; expected " + width);
        }
    }

    private static String names(List<Column> key) {
        return String.join(", ", key.stream().map(Column::name).toList());
    }
}
