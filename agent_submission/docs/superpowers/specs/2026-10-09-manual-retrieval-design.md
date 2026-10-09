# Manual retrieval design

## Goal

Use the Scan User Manual's section structure to retrieve useful passages for dofile generation and repair without a model, network call, or new runtime dependency.

## Evidence from the supplied manual

The 88-page PDF extracts as searchable Chinese and English text with `pypdf`. Repeated page headers and dates enter every page. Chinese headings such as `设置测试使能信号` and `删除 Scan Chain` are missed by the current heading expression. The current `scan` plus `insert_dft_logic` query returns five passages without `insert_dft_logic` in its top six. Empty queries return unrelated command passages.

## Design

1. Remove repeated manual headers, page dates, and table-of-contents leader lines during page extraction. Keep the original page number for citations.
2. Split on numbered headings, short Chinese headings, and Markdown headings. Do not treat a command-only example line or a single `#` script comment as a heading. Store the active heading with every chunk and carry it across page boundaries. Cap each body at 2,000 characters.
3. Tokenize ASCII identifiers as complete, case-insensitive tokens, preserving underscores and numeric DRC rule suffixes. Tokenize Chinese text into overlapping bigrams. Compute BM25F over title and body with a higher title weight, and give an additional bounded boost only to an exact queried command identifier. Return no passages for an empty query or zero matching score. Break ties by PDF page and chunk index.
4. Convert generation requirements into focused query groups for setup, signals, chain configuration, DRC, insertion, and reports/output. Give each supplied CTL, Wrapper, Partition, Segment, Lockup, allowed DRC rule, and extra output topic its own group. Convert repair diagnostics into a group per distinct rule or command; fall back to generation groups if no diagnostic identifier exists. Select one passage per group before filling remaining places, deduplicating passages. Keep six passages for ordinary tasks and allow up to fourteen when extra topics need coverage. Place the six base phase groups first, then special command and required output groups, then individual DRC rules, so the bounded prompt retains the required output instructions even when many rules are supplied.
5. Keep the existing `ManualChunk(page, chunk_index, text)` construction valid by giving the heading field a default. Include the heading in model-facing manual data while preserving page and chunk references. Keep deadline checks and the keyword-only audit category.

## Validation

- Unit tests cover Chinese headings, header removal, cross-page heading inheritance, exact command boundaries, title weight, BM25 length normalization, empty/no-match queries, and deterministic ties.
- Workflow tests cover generation group coverage and repair queries without diagnostic identifiers.
- An optional local test reads the supplied PDF when present and checks ordinary and special-topic coverage plus direct DRC-rule definitions. The PDF is not copied into the submission image.
- Run the complete offline test suite and compare the real-manual retrieval against the current baseline. No claim is made about real EDA success without a licensed tool run.
