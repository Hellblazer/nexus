/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.List;

/**
 * RDR-231: the engine's one note source_uri rule. A note is {@code nx-note://<collection>/<rest>}
 * (stored) or the legacy {@code chroma://<collection>/<rest>} (accepted on input forever), whose
 * collection segment is not file-routed. {@code chroma://rdr__...}, {@code docs__...} and
 * {@code code__...} values are residue from indexing, never notes, and keep their spelling.
 * The shared corpus {@code note-uri-corpus.json} pins this class, the client's
 * {@code aspect_readers} helpers and the backfill predicate to the same answers.
 */
public final class NoteUri {

    /** The client's {@code aspect_readers.FILE_ROUTED_PREFIXES}; the corpus file holds the list both check. */
    public static final List<String> FILE_ROUTED_PREFIXES = List.of("rdr__", "docs__", "code__");

    public static final String LEGACY = "chroma://";
    public static final String NOTE = "nx-note://";
    private static final String EXPORT = "nxexp://";

    private NoteUri() {}

    /** True for a note URI in either spelling, matching RDR-231 step 5's SQL predicate. */
    public static boolean isNote(String uri) {
        if (uri == null) {
            return false;
        }
        String rest;
        if (uri.startsWith(LEGACY)) {
            rest = uri.substring(LEGACY.length());
        } else if (uri.startsWith(NOTE)) {
            rest = uri.substring(NOTE.length());
        } else {
            return false;
        }
        return !fileRouted(rest);
    }

    /**
     * The stored spelling: a legacy note URI becomes {@code nx-note://}, as does a legacy note
     * wrapped by an export identity {@code nxexp://<target>/chroma://<c>/...}. Every other value,
     * file-routed residue and null included, comes back unchanged.
     */
    public static String canonical(String uri) {
        if (uri == null) {
            return null;
        }
        if (uri.startsWith(LEGACY)) {
            return isNote(uri) ? NOTE + uri.substring(LEGACY.length()) : uri;
        }
        if (uri.startsWith(EXPORT)) {
            int slash = uri.indexOf('/', EXPORT.length());
            if (slash >= 0) {
                String inner = uri.substring(slash + 1);
                if (inner.startsWith(LEGACY) && isNote(inner)) {
                    return uri.substring(0, slash + 1) + NOTE + inner.substring(LEGACY.length());
                }
            }
        }
        return uri;
    }

    /**
     * The source_uri of a row moving from collection {@code oldColl} to {@code newColl}, for rename
     * and re-home: {@code nx-note://<old>/} becomes {@code nx-note://<new>/}, and only when {@link #isNote} classes
     * the value as a note, which is to say when {@code oldColl} carries no file-routed prefix. Every other value is
     * unchanged: {@code chroma://} residue and the legacy spelling (which becomes a stale note that the repair
     * rewrites), an {@code nx-note://} value with a file-routed segment (moving it onto a prefix-free label would turn
     * it into a note), and {@code nxexp://} values. RDR-229 P2.10; the plpgsql twin is
     * {@code nexus.catalog_reconcile_commit} (catalog-049).
     */
    public static Field<String> moveTo(Field<String> uri, String oldColl, String newColl) {
        if (segmentFileRouted(oldColl)) {
            return uri;
        }
        String oldNote = NOTE + oldColl + "/";
        return DSL.when(startsWith(uri, DSL.val(oldNote)),
                DSL.val(NOTE + newColl + "/").concat(DSL.substring(uri, oldNote.length() + 1)))
            .otherwise(uri);
    }

    /**
     * As {@link #moveTo(Field, String, String)}, with the old collection read from the row (the
     * collection update, where each row's current collection is the old one). The file-routed test is SQL on that
     * column, {@code starts_with} per prefix and not LIKE, because {@code _} is a LIKE wildcard.
     */
    public static Field<String> moveTo(Field<String> uri, Field<String> oldColl, String newColl) {
        Field<String> oldNote = DSL.val(NOTE).concat(oldColl).concat(DSL.val("/"));
        return DSL.when(startsWith(uri, oldNote).and(notFileRouted(oldColl)),
                DSL.val(NOTE + newColl + "/").concat(
                    DSL.substring(uri, DSL.charLength(oldColl).plus(NOTE.length() + 2))))
            .otherwise(uri);
    }

    /** {@link #moveTo(Field, String, String)} on one value, for a pre-check computed outside SQL. */
    public static String moveTo(String uri, String oldColl, String newColl) {
        if (uri == null || segmentFileRouted(oldColl)) {
            return uri;
        }
        String prefix = NOTE + oldColl + "/";
        return uri.startsWith(prefix) ? NOTE + newColl + "/" + uri.substring(prefix.length()) : uri;
    }

    /** True for a URI that {@link #moveTo} would rewrite out of {@code coll}: exactly its {@code nx-note://} notes. */
    public static Condition namesCollection(Field<String> uri, String coll) {
        if (segmentFileRouted(coll)) {
            return DSL.falseCondition();
        }
        return startsWith(uri, DSL.val(NOTE + coll + "/"));
    }

    /** SQL: {@code coll} carries none of {@link #FILE_ROUTED_PREFIXES} (a null column is unknown, so it matches no row). */
    private static Condition notFileRouted(Field<String> coll) {
        Condition routed = null;
        for (String p : FILE_ROUTED_PREFIXES) {
            Condition one = startsWith(coll, DSL.val(p));
            routed = routed == null ? one : routed.or(one);
        }
        return routed.not();
    }

    /**
     * SQL pre-filter of the stale-note repair (nexus-bivz3.24): the row's URI is in a note spelling and does NOT open
     * with its own collection ({@code <scheme><coll>/}). A superset of the stale notes: file-routed residue and a URI
     * with no title part also pass, and {@link #isNote} / the repair's Java classification settle them.
     */
    public static Condition notInOwnCollection(Field<String> uri, Field<String> physicalCollection) {
        // A null collection would make the concatenation null and "own" unknown, which NOT turns into unknown: the
        // row would pass no filter and so be invisible. Read as blank, it is a row that names no collection of its
        // own, which the repair classifies as malformed.
        Field<String> coll = DSL.coalesce(physicalCollection, DSL.val(""));
        Condition noteScheme = startsWith(uri, DSL.val(NOTE)).or(startsWith(uri, DSL.val(LEGACY)));
        Condition own = startsWith(uri, DSL.val(NOTE).concat(coll).concat(DSL.val("/")))
            .or(startsWith(uri, DSL.val(LEGACY).concat(coll).concat(DSL.val("/"))));
        return noteScheme.and(own.not());
    }

    /** The collection segment of a note URI in either spelling, or null when {@code uri} is not in a note spelling. */
    public static String collectionOf(String uri) {
        if (uri == null) {
            return null;
        }
        String rest;
        if (uri.startsWith(NOTE)) {
            rest = uri.substring(NOTE.length());
        } else if (uri.startsWith(LEGACY)) {
            rest = uri.substring(LEGACY.length());
        } else {
            return null;
        }
        int slash = rest.indexOf('/');
        return slash < 0 ? rest : rest.substring(0, slash);
    }

    /**
     * The stored spelling of {@code uri} filed under {@code newColl} when it names {@code oldColl}:
     * {@code nx-note://<old>/rest} and {@code chroma://<old>/rest} both become {@code nx-note://<new>/rest}. Any other
     * value, and a note naming a different collection, comes back unchanged. The repair's per-row rewrite, where the
     * old collection varies by row, so no SQL expression of a single literal fits.
     */
    public static String retarget(String uri, String oldColl, String newColl) {
        if (uri == null) {
            return null;
        }
        for (String scheme : List.of(NOTE, LEGACY)) {
            String prefix = scheme + oldColl + "/";
            if (uri.startsWith(prefix)) {
                return NOTE + newColl + "/" + uri.substring(prefix.length());
            }
        }
        return uri;
    }

    /** Postgres {@code starts_with}: a literal prefix test, unlike LIKE where '_' matches any char. */
    static Condition startsWith(Field<String> uri, Field<String> prefix) {
        return DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, uri, prefix));
    }

    private static boolean fileRouted(String afterScheme) {
        int slash = afterScheme.indexOf('/');
        return segmentFileRouted(slash < 0 ? afterScheme : afterScheme.substring(0, slash));
    }

    /**
     * The URI-segment test of {@link #isNote}: the segment opens with a frozen file-routed prefix. {@link #moveTo} and
     * {@link #namesCollection} use this, not {@link #isFileRoutedCollection}: the old collection IS the URI's segment
     * ({@code nx-note://<old>/}), so they classify exactly what {@code isNote} classifies, and they do not move with the
     * live-collection test that RDR-229 P2.9 repoints to the registry row's content type.
     */
    private static boolean segmentFileRouted(String segment) {
        if (segment == null) {
            return false;
        }
        for (String p : FILE_ROUTED_PREFIXES) {
            if (segment.startsWith(p)) {
                return true;
            }
        }
        return false;
    }
}
