/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.jooq.binding;

import java.util.Arrays;

/**
 * Immutable pgvector value (nexus-xtmtf) — the jOOQ user type for the
 * {@code vector} column type (see {@link VectorBinding}).
 *
 * <p>A wrapper class rather than a bare {@code float[]} because jOOQ's
 * generated POJOs cannot handle primitive-array user types (their
 * equals/hashCode generation casts to {@code Object[]}); this mirrors the
 * shape of jOOQ's commercial-only {@code FloatVector}.
 */
public final class Vector {

    private final float[] floats;

    private Vector(float[] floats) {
        this.floats = floats;
    }

    /** Wrap (no copy — treat the array as frozen after handing it over). */
    public static Vector of(float[] floats) {
        return floats == null ? null : new Vector(floats);
    }

    public float[] floats() {
        return floats;
    }

    /** pgvector text form: {@code [f1,f2,...]}. */
    @Override
    public String toString() {
        StringBuilder sb = new StringBuilder(floats.length * 8).append('[');
        for (int i = 0; i < floats.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(floats[i]);
        }
        return sb.append(']').toString();
    }

    /** Parse the pgvector text form. */
    public static Vector parse(String text) {
        String body = text.trim();
        if (body.startsWith("[")) body = body.substring(1);
        if (body.endsWith("]")) body = body.substring(0, body.length() - 1);
        if (body.isBlank()) return new Vector(new float[0]);
        String[] parts = body.split(",");
        float[] out = new float[parts.length];
        for (int i = 0; i < parts.length; i++) {
            out[i] = Float.parseFloat(parts[i].trim());
        }
        return new Vector(out);
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof Vector v && Arrays.equals(floats, v.floats);
    }

    @Override
    public int hashCode() {
        return Arrays.hashCode(floats);
    }
}
