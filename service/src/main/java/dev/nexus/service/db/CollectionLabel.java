// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * The minted collection label (RDR-229 item 8): {@code c} followed by decimal digits. It is reserved to
 * the engine, so the label rule (P3.6) refuses a user label of this form, and the minter (P3.8) renders
 * it. Both read this one class, so the form they agree on cannot drift.
 */
public final class CollectionLabel {

    private CollectionLabel() {
    }

    /**
     * Whether {@code label} is of the minted form: exactly {@code c} followed by one or more ASCII digits.
     * Leading zeros count ({@code c007} is reserved too), so a user cannot hold a near-twin of a minted
     * label. Case is not folded and no non-ASCII digit counts.
     */
    public static boolean isMintedForm(String label) {
        if (label == null || label.length() < 2 || label.charAt(0) != 'c') {
            return false;
        }
        for (int i = 1; i < label.length(); i++) {
            char ch = label.charAt(i);
            if (ch < '0' || ch > '9') {
                return false;
            }
        }
        return true;
    }

    /** The minted label for {@code id}: {@code "c" + id}, never padded. {@code id} must be positive. */
    public static String mintedLabel(long id) {
        if (id <= 0) {
            throw new IllegalArgumentException("a minted label's id must be positive, got " + id);
        }
        return "c" + id;
    }

    /**
     * RDR-229 item 7, the label rule's one pending clause: a label holding {@code '/'} is refused. PENDING Sam's
     * ruling (board ask {@code rdr229-label-slash}): {@code NoteUri.collectionOf} ends a collection at the first
     * {@code '/'}, so such a label would mis-parse {@code nx-note://<label>/...}, and no audit yet covers where a
     * label meets a file path. If he allows {@code '/'}, set this {@code false} (and the client's
     * {@code LABEL_REFUSES_SLASH}) and change {@code CollectionLabelTest.aSlashIsRefusedUntilSamRulesOnIt}.
     */
    static final boolean REFUSE_SLASH = true;

    /** The most UTF-8 bytes a label may hold (RDR-229 item 7). */
    static final int MAX_LABEL_BYTES = 255;

    /**
     * Refuses a label a USER supplied, as an {@link IllegalArgumentException} (the engine's 400): the label rule of
     * RDR-229 item 7, twin of the client's {@code validate_collection_name}. The rule is: non-empty, valid UTF-8
     * (no lone surrogate), no control character, at most {@value #MAX_LABEL_BYTES} UTF-8 bytes, not of the minted
     * form ({@link #isMintedForm}, so a user cannot claim a label a later mint would produce), and no {@code '/'}
     * while {@link #REFUSE_SLASH} holds.
     *
     * <p>Control characters are the Unicode categories Cc (the C0 and C1 controls, NUL and DEL among them), Zl and Zp
     * (U+2028 and U+2029, which break a line in a terminal, a log or a note URI). Cf is allowed on purpose: the
     * zero-width joiner and non-joiner spell words in Persian and the Indic scripts and join emoji sequences, and a
     * label is not a security principal (uniqueness is on the exact string), so a bidi override can no more spoof
     * it than a homoglyph can, which no rule catches either.
     *
     * <p>Called only where a user's label enters: the client route's registration of a NEW row and the rename
     * target. The engine's own minter (P3.8) renders its label with {@link #mintedLabel} and never passes through
     * here, so it stays able to write the form this rule refuses.
     */
    public static void validateUserLabel(String label) {
        validateShape(label);
        requireNotMinted(label);
    }

    /**
     * The label rule minus the minted-form clause: non-empty, valid UTF-8, no control character, at most
     * {@value #MAX_LABEL_BYTES} bytes, no {@code '/'} while {@link #REFUSE_SLASH} holds. Split out so the
     * registration route can run it before it reads the database (a NUL bound into a lookup is a database error, not
     * a 400) while applying {@link #requireNotMinted} to a NEW label only.
     */
    static void validateShape(String label) {
        if (label == null || label.isEmpty()) {
            throw new IllegalArgumentException("a collection label must not be empty");
        }
        int bytes = 0;
        for (int i = 0; i < label.length(); ) {
            int cp = label.codePointAt(i);
            int n = Character.charCount(cp);
            if (cp >= Character.MIN_SURROGATE && cp <= Character.MAX_SURROGATE) {
                throw new IllegalArgumentException("collection label " + quoted(label) + " is not valid UTF-8 "
                    + "(a lone surrogate)");
            }
            int type = Character.getType(cp);
            if (type == Character.CONTROL || type == Character.LINE_SEPARATOR || type == Character.PARAGRAPH_SEPARATOR) {
                throw new IllegalArgumentException("collection label " + quoted(label)
                    + " holds a control character (U+" + String.format("%04X", cp) + ")");
            }
            bytes += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
            i += n;
        }
        if (bytes > MAX_LABEL_BYTES) {
            throw new IllegalArgumentException("collection label " + quoted(label.length() > 40 ? label.substring(0, 40) : label)
                + "... is " + bytes + " bytes in UTF-8; the limit is " + MAX_LABEL_BYTES);
        }
        if (REFUSE_SLASH && label.indexOf('/') >= 0) {
            throw new IllegalArgumentException("collection label " + quoted(label) + " holds '/', which a label may not hold");
        }
    }

    /** Refuses a user label of the minted form: the engine reserves {@code c<digits>} for the labels it mints. */
    static void requireNotMinted(String label) {
        if (isMintedForm(label)) {
            throw new IllegalArgumentException("collection label " + quoted(label) + " is of the minted form (c followed by "
                + "digits), which the engine reserves for the labels it mints");
        }
    }

    private static String quoted(String label) {
        return "'" + label + "'";
    }
}
