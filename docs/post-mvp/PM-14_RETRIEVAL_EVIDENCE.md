# PM-14 Retrieval Evidence

## Confirmed lexical miss (fixed by AIINBOX-1)

- Query: `Андроид`
- Existing saved corpus: contains relevant Items using `Android`.
- Previously, lexical retrieval did not return those Items for the Russian
  spelling.
- User impact: a materially relevant saved result is absent, with no indication
  in the previous empty-search response that vocabulary mismatch caused it.

Search and Ask now support the explicit bidirectional spelling pair on FTS5.
This historical miss no longer demonstrates a gap in the current retrieval path.
PM-14 runtime work remains on hold until a separate explicit decision based on
real usage.
