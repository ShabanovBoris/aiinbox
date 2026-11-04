# PM-14 Retrieval Evidence

## Confirmed lexical miss

- Query: `Андроид`
- Existing saved corpus: contains relevant Items using `Android`.
- Observed behavior: SQLite FTS5 does not return those Items for the Russian
  spelling because the current retrieval path matches indexed words rather than
  translating or transliterating them.
- User impact: a materially relevant saved result is absent, with no indication
  in the previous empty-search response that vocabulary mismatch caused it.

This is evidence for PM-14's hybrid retrieval gate. POLISH-07 discloses the
lexical limitation in Search and Ask; it does not add aliases, embeddings, or
semantic retrieval. POLISH-08 is complete. PM-14 runtime work remains on hold
until a separate explicit decision based on real usage.
