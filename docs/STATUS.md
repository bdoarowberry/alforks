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

## Fixed after feedback round 3

- Worker processes no longer print SQLGlot fallback warnings, which quoted raw SQL.
- **Parse repairs:**
  - spaced comparison operators (`! =`, `< =`, `> =`);
  - `PRIMARY KEY` / `UNIQUE CLUSTERED` inside CREATE TABLE;
  - several DECLAREs on one line.
- `ALTER TABLE … ADD PRIMARY KEY/UNIQUE/…` and index-option fragments count as non-lineage.
- **"Where proof ends"** is comprehensive:
  - every stop and every node missing proof is its own bullet, with its location, the step(s) and
    branch(es) that reach it, and the next evidence needed;
  - it says explicitly when no value path reaches the requested boundary;
  - weaker (inferred/partial) links are listed;
  - the tracer guarantees every value-path end has a stop or a gap: otherwise an `unexplained_end`
    gap is recorded.

## Added after feedback round 4

- Variables whose names start with a digit (`@2_periods_ago`) parse; their names are kept.
- `--assertions FILE`: human-asserted mappings for dynamic SQL and external objects. Asserted steps
  are marked `asserted`, explained gaps move to `resolved_gaps`, and completeness reports how far
  the result relies on assertions.

## Added after feedback round 5

- `--local-server NAME`: linked-server references resolve to scanned local databases.
- Pages state reliance on assertions at the top and count value-path, context and row-selection
  gaps separately.
- "Where proof ends" lists the statements behind the most gaps.
- `scan-diagnostics.json` lists `assertion_effects`; assertion evidence uses the real file path.

## Fixed after feedback round 6

- Row-selection clauses no longer expand CTEs and derived tables (unless `--trace-row-selection`):
  their upstream steps and gaps were traced off the value path, inflating step and gap counts.
- Dynamic SQL that can only execute an empty string or NULL is reported as `dead_dynamic_sql` in
  scan diagnostics, not as a gap.
- "Statements behind the most gaps" counts only gaps that cite a statement.
- Assertion evidence paths are relative to the output folder (where the pages are).

## Fixed after feedback round 7 (75-column batch)

- **A writer after an IF condition on the same line was silently dropped.** For
  `IF NOT EXISTS (SELECT …) INSERT …`, the indexer starts at the inner SELECT; the fragment was
  classed as non-lineage, so the target looked like a source with no writer and the trace could
  claim "complete". The statement after the condition is now recovered, with its IF context.
- Text that names a write target is never classed as non-lineage.
- `compare`: `no_baseline` for columns without expected sources.
- Diagnostics rank the statements blocking the most traces, with their locations.

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
