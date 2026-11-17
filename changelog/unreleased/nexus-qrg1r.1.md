### Added

- **`nx catalog span` prints a `dchash:` span for a passage of a document.** `nx catalog span TUMBLER --passage "quoted text"` finds the passage in the document's source file and prints its span, ready for `nx catalog link --from-span` or `--to-span`; `--start N --end N` gives offsets instead (Unicode code points in the file's text with CRLF read as LF). A passage that is empty, absent or occurs more than once is refused, and so are PDFs, `nx-note://` notes, documents with no source file, and files that are not valid UTF-8.
