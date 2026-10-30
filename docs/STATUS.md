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

## Known open defects (fix first)

1. **False `complete` when the only writer is unparsed or dynamic and its write target is an alias.**
   - Examples:
     - `UPDATE TOP (5000) f SET ... FROM mart.x AS f` (does not parse);
     - dynamic SQL `'UPDATE f SET ... FROM mart.x AS f'`.
   - The write target is recorded as `f`, not `x`, so no gap is attached to `x`.
   - Planned fix:
     - strip `TOP (n)` after UPDATE, DELETE and INSERT and re-parse (extend
       `Catalog._rewrite_compound`);
     - resolve `alias -> table` from `FROM/JOIN name [AS] alias` in `write_targets`, for
       both unparsed statements and dynamic-SQL literals.
   - Corpus chains 10 and 13 reproduce it.
2. **sqlglot warnings on stderr.** It logs "Falling back to parsing as a 'Command'" for every
   unparsed statement. Set `logging.getLogger("sqlglot").setLevel(logging.ERROR)` in `cli.main`.
3. **Corpus chain 08** ends in `missing_definition` with recall 0.6. Not investigated yet.

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
