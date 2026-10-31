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

## Added: static evaluation of dynamic SQL (prototype)

- `src/column_lineage/dynamic_eval.py`: dynamic SQL whose every possible text is built from literals
  (also via variables, IF/ELSE, parameters with literal callers or defaults; at most 16 alternatives)
  is parsed and analysed as writers at the EXEC; the steps are `inferred`. See the design doc,
  "Dynamic SQL evaluation". Fixtures: `fixtures/column/dynamic/`; tests: `tests/test_column_dynamic_eval.py`.
- Parameter defaults that are string literals (`@p sysname = N'x',`) were cut to `N`; fixed.

## Fixed after adversarial review 4 (tests: tests/test_column_review4.py)

- Glued-statement splitting is accepted only when every piece is a whole statement (a line-start
  `WITH (NOLOCK)` hint no longer splits one UPDATE into a phantom writer and a lost tail).
- `OUTPUT … INTO` parsed natively, `ALTER TABLE … SWITCH`, unparsed writes through synonyms and
  views, and writes through views with several definitions are reported as gaps.
- Alias resolution for unparsed/dynamic targets uses every table an alias names.
- Targets read from string literals need INSERT/UPDATE/MERGE or SELECT … INTO; reserved words
  are never targets; runtime-built dynamic SQL keeps its scope gap (one gap per statement).
- `ALTER TABLE … ADD DEFAULT … FOR col` defaults; `.WRITE` updates; TOP (n) notes; compound
  assignment precedence; `WITH EXECUTE AS` headers; digit-variable placeholders cannot capture
  real names; functions in WHERE are row-selection gaps.
- Assertions: `writer.path` must match exactly (optionally prefixed by the root label);
  malformed `writer`/`resolves` are usage errors.
- `compare`: sources reached only through ambiguous bindings are `found_as_ambiguous_candidate`.

## Fixed after feedback round 8

- A no-writer stop now says what the writer search found (`writer_search` on the stop, counted
  in `gap-summary.json` under `totals.no_writer_stops`). In synthetic tests every shape of
  `INSERT ... SELECT` inside a procedure body traces correctly; the reproduced cause of the
  symptom is a writer whose database context (USE, `--database-map`, `--default-database`)
  resolves its target to another database than the reader's. Tests: `tests/test_column_round8.py`.
- **Root cause of the missed writer (C23): file encoding.** Files saved as UTF-16 (SSMS
  "Unicode") were decoded as UTF-8, so every statement in them silently vanished and their
  writers' targets looked like sources with no writer. Files are now decoded by BOM / NUL
  pattern (UTF-16 LE/BE, UTF-8, else Windows-1252); the scan reports `file_encodings` and
  `files_without_statements`. Confirmed on the real sources: 15 of 1,438 files were UTF-16 with a
  BOM; reading them added 309 statements, and the missed writer is now traced. The writer search now also reports writers of the same object that
  do not name the column, and statements that only wrote another object.
- `completeness.result` and `ends_by_meaning` distinguish reaching the requested boundary from
  being complete only within the selected sources.
- Parse repairs: `@x + = y`, qualified column names in an INSERT into a table variable, index
  options glued to the next statement, a lone `WITH EXECUTE AS` header (left an empty statement),
  `db..name` in text-matched targets (it was read as the database name).
- Parse-error classes keep SQLGlot's own rule names (`Required keyword: high missing for Between`).

## Fixed after the 178-column SQL batch

- Compound assignment ending in a string literal (`SET @sql += N'...'`, `SELECT @c += ',' + name + ']'`)
  was rewritten with the final literal cut off (the masked text is blank there), so the statement
  failed to parse; dynamic SQL built that way could not be evaluated. Test:
  `test_compound_assignment_ending_in_a_literal`.
- Parse-error classes now really keep SQLGlot's rule names (the stored message has a prefix).
- Variables named like keywords (`@End`, `@Update`, `@Set`, `@Declare` …) started a new statement at
  the keyword, cutting the statement after `@` (shared indexer, `table_lineage/index.py`). This was
  behind most remaining "Required keyword: this missing for Parameter" and "DECLARE <name>" fallbacks;
  writers among them became parse errors. Test: `test_keyword_named_variables_do_not_split_statements`.

## Added: optional SSIS adapter

- `--package-root`, `--package-include/--package-exclude`, `--connection-map`; modules `ssis.py`,
  `ssis_xml.py`, `ssis_expression.py`; contract in the design doc ("SSIS packages").
- An adversarial review found 3 realistic false edges (error outputs, variable scoping, cache
  Lookup), 2 binding-order false edges and several hidden runtime unknowns; all fixed with
  regression fixtures in `fixtures/column/ssis_review/` (`tests/test_column_ssis_review.py`).
  Runtime values (parameters, system/expression variables, configurations) are gaps, not stops.
- First real run (one production project, 3 packages): the file-to-staging column traces through
  conversion to the file column, all three links verified against the XML by the tester; unmapped
  table columns stay `package_column_not_mapped`. Fixed after it: disabled tasks no longer add
  context gaps to traces through their package; `gap-summary.json` lists anonymized shapes of SSIS
  expressions the parser rejects (`packages.unparsed_expression_samples`); `compare` accepts file
  sources (`<file connection>.<column>`) and counts file stops as reached ends.
- Raw SSIS expressions bracket function names (`[LEN](#{...}) == 0`); these are now parsed as function
  calls (this was the Conditional Split row-selection gap on the real package). Expression shapes in
  the gap summary tag reference kinds (`[IDn]`, `#IDn`, `@IDn`).
- Filegroup clauses on their own (`ON [PRIMARY]`) are non-lineage.

## Known open issues

- Corpus chain 08 ends in `missing_definition` (recall 0.6). Not investigated.
- Dynamic SQL that builds its projection from metadata cannot be followed. This is by design: it is
  reported as a `dynamic_sql` gap at the EXEC (its detail now says which piece is unknown).
- A dynamic SQL issue's literal "targets" are guessed from all string literals in its scope; text like
  `'INSERT INTO ' + @t` yields the bogus target `into`, and an issue with any target is not reported as a
  scope-level gap. Unchanged by the evaluator (it only matters when evaluation is incomplete).

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
