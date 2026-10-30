# Column lineage: status and next steps

## Where it stands

- Engine: `src/column_lineage/` (catalog, control, analyzer, tracer, cli, render_markdown, gap_summary).
- Contract: `docs/column-lineage-design.md`. Usage: `README.md`.
- Tests: 97 passing (`python -m pytest -p no:cacheprovider`), including:
  - 46 acceptance and messy-pattern tests;
  - 20 renderer tests;
  - 24 regressions from an adversarial semantics review (`fixtures/column/review/`);
  - 7 corpus-generator tests.
- Synthetic ~3,000-object corpus (`tools/generate_corpus.py --objects 3000 --seed 42`), 20 known chains:
  - precision 1.0, static recall 0.975;
  - about 20 s per run, most of it parsing.

## Fixed after the first real-source test (feedback round 1)

- **False `complete` with an aliased unparsed or dynamic writer** (`UPDATE TOP (n) f … FROM x AS f`):
  - `TOP (n)` is blanked and the statement re-parsed;
  - aliases are resolved to tables for unparsed statements and dynamic-SQL literals.
- **Statement repair:**
  - INSERT table hints, `OPTION (...)` query hints and `OUTPUT … INTO` are blanked (the OUTPUT
    target is reported as a gap);
  - statements the indexer glued together (missing semicolon before TRUNCATE, WHILE, SET, …) are
    split and parsed piece by piece;
  - an INSERT cut off from its SELECT is rejoined.
- **Non-lineage statements** (RAISERROR, SET options, EXECUTE AS, IF-condition fragments) are counted
  as `ignored_non_lineage`, not as parse failures.
- **Gaps are classified** as `value`, `row_selection` or `context` (`affects` in the JSON):
  - completeness depends only on value-path and context gaps;
  - row-selection gaps are reported separately.
- **Report text:** SQL comments no longer leak into labels or excerpts; "and and N more" fixed.
- **Windows:** `render_markdown -o -` writes UTF-8 bytes (it used to raise UnicodeEncodeError on cp1252).
- **sqlglot fallback warnings** are silenced.
- **Anonymizer:** `@var` / `#temp` stay single tokens, and built-in function names are kept.

## Known open issues

- Corpus chain 08 ends in `missing_definition` (recall 0.6). Not investigated.
- Dynamic SQL that builds its projection from metadata cannot be followed. This is by design: it is
  reported as a `dynamic_sql` gap at the EXEC.

## Still to do

- Robustness review: a reviewer was checking encodings, huge files, deep nesting and symlink
  loops, but its results were not collected. Rerun it.
- Performance: parsing dominates. Options:
  - parallel parse (`--jobs`);
  - a pickle cache (about 2.4x on small files);
  - a cheaper `mask_noncode` in `table_lineage/index.py`, which runs 4–5 times per statement.
- Renderer suggestions not yet done:
  - a structured pairing between a CASE condition and the value it selects;
  - a per-step scope field;
  - gap-to-step links.
- `docs/column-lineage-limitations.md`: referenced from the README but not written yet.
- Handback package for the tester LLM:
  - how to run;
  - how to send back `gap-summary.json`.
