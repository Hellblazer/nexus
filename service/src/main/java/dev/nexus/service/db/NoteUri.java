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
     * and re-home: {@code nx-note://<old>/} becomes {@code nx-note://<new>/} and
     * {@code chroma://<old>/} becomes {@code chroma://<new>/}. Every other value is unchanged, so
     * residue keeps its spelling and {@code nxexp://} values are not touched.
     */
    public static Field<String> moveTo(Field<String> uri, String oldColl, String newColl) {
        String oldNote = NOTE + oldColl + "/";
        String oldLegacy = LEGACY + oldColl + "/";
        return DSL.when(startsWith(uri, DSL.val(oldNote)),
                DSL.val(NOTE + newColl + "/").concat(DSL.substring(uri, oldNote.length() + 1)))
            .when(startsWith(uri, DSL.val(oldLegacy)),
                DSL.val(LEGACY + newColl + "/").concat(DSL.substring(uri, oldLegacy.length() + 1)))
            .otherwise(uri);
    }

    /**
     * As {@link #moveTo(Field, String, String)}, with the old collection read from the row (the
     * collection update, where each row's current collection is the old one).
     */
    public static Field<String> moveTo(Field<String> uri, Field<String> oldColl, String newColl) {
        Field<String> oldNote = DSL.val(NOTE).concat(oldColl).concat(DSL.val("/"));
        Field<String> oldLegacy = DSL.val(LEGACY).concat(oldColl).concat(DSL.val("/"));
        return DSL.when(startsWith(uri, oldNote),
                DSL.val(NOTE + newColl + "/").concat(
                    DSL.substring(uri, DSL.charLength(oldColl).plus(NOTE.length() + 2))))
            .when(startsWith(uri, oldLegacy),
                DSL.val(LEGACY + newColl + "/").concat(
                    DSL.substring(uri, DSL.charLength(oldColl).plus(LEGACY.length() + 2))))
            .otherwise(uri);
    }

    /** {@link #moveTo(Field, String, String)} on one value, for a pre-check computed outside SQL. */
    public static String moveTo(String uri, String oldColl, String newColl) {
        if (uri == null) {
            return null;
        }
        for (String scheme : List.of(NOTE, LEGACY)) {
            String prefix = scheme + oldColl + "/";
            if (uri.startsWith(prefix)) {
                return scheme + newColl + "/" + uri.substring(prefix.length());
            }
        }
        return uri;
    }

    /** True for a URI that {@link #moveTo} would rewrite out of {@code coll}, in either note spelling. */
    public static Condition namesCollection(Field<String> uri, String coll) {
        return startsWith(uri, DSL.val(NOTE + coll + "/")).or(startsWith(uri, DSL.val(LEGACY + coll + "/")));
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

    /** True when the collection name opens with a file-routed prefix ({@code rdr__}, {@code docs__}, {@code code__}). */
    public static boolean isFileRoutedCollection(String collection) {
        if (collection == null) {
            return false;
        }
        for (String p : FILE_ROUTED_PREFIXES) {
            if (collection.startsWith(p)) {
                return true;
            }
        }
        return false;
    }

    /** Postgres {@code starts_with}: a literal prefix test, unlike LIKE where '_' matches any char. */
    static Condition startsWith(Field<String> uri, Field<String> prefix) {
        return DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, uri, prefix));
    }

    private static boolean fileRouted(String afterScheme) {
        int slash = afterScheme.indexOf('/');
        String segment = slash < 0 ? afterScheme : afterScheme.substring(0, slash);
        for (String p : FILE_ROUTED_PREFIXES) {
            if (segment.startsWith(p)) {
                return true;
            }
        }
        return false;
    }
}
