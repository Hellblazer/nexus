/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.Converter;
import org.jooq.DataType;
import org.jooq.Field;
import org.jooq.TableField;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.HexFormat;

/**
 * RDR-180 (nexus-jxizy.7): the jOOQ seam of the hex-interchange discipline.
 *
 * <p>Chash-bearing columns are stored as {@code bytea} (32 raw bytes — the
 * full SHA-256); Java-side plumbing carries the 64-lowercase-hex interchange
 * form, already boundary-validated by the {@link Chash} type at the HTTP
 * seam. This converted data type makes the encode/decode invisible and
 * UNIFORM: binds hex→bytes, fetches bytes→hex, at every site that reads or
 * writes a chash-bearing column through it — no per-site encode/decode, no
 * chance of a site forgetting one direction.
 *
 * <p>Width-agnostic BY DESIGN: not-yet-rekeyed legacy rows (16-byte decoded
 * pre-RDR-180 values, or ETL-era ids carried as UTF-8 bytes) round-trip
 * through hex faithfully; width enforcement lives in the {@link Chash}
 * boundary type and the DB {@code octet_length} CHECKs, not in this codec.
 *
 * <p>Ordering note: lowercase-hex lexicographic order equals unsigned byte
 * order, so {@code ORDER BY} / sort-for-lock-order semantics are unchanged
 * on either side of the seam.
 */
public final class ChashHex {

    private ChashHex() {
    }

    /** bytea column carried as its hex rendering in Java. */
    public static final DataType<String> HEX_TYPE =
        SQLDataType.BLOB.asConvertedDataType(Converter.ofNullable(
            byte[].class, String.class,
            b -> HexFormat.of().formatHex(b),
            h -> HexFormat.of().parseHex(h)));

    /**
     * A hex-typed reference to a generated {@code byte[]} chash-bearing
     * column — use in selects/wheres/inserts where the Java value is the
     * hex interchange form.
     */
    public static Field<String> hex(TableField<?, byte[]> field) {
        return DSL.field(field.getQualifiedName(), HEX_TYPE);
    }

    /** Same, addressed by table + column name (the DimTables wrapper shape). */
    public static Field<String> hex(org.jooq.Table<?> table, String column) {
        Field<?> f = table.field(column);
        if (f == null) {
            throw new IllegalStateException(
                "no column '" + column + "' on " + table.getQualifiedName());
        }
        return DSL.field(f.getQualifiedName(), HEX_TYPE);
    }
}
