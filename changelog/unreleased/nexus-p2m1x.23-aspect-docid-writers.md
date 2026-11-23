### Changed

- The aspect salient-sentence update and the highlight delete that name a row by `doc_id` alone (`setSalientSentences`, `deleteHighlight`) read the registry rows of the collections that hold the doc's rows `FOR KEY SHARE` in their own write transaction, by each row's stored `collection_id` in id order, before they write (RDR-229 P3.5a). A retire, rename or supersede of one of those collections now waits for the write, and a write that meets one waits for it and follows a row it re-homed; each row keeps its own id and label. No wire change.
