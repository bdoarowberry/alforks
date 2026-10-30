# Build brief: generic SQL column lineage analyzer

## Goal

Extend the existing Python table lineage analyzer into a reusable **column lineage analyzer**. Given any target object and column in a set of local SQL files, trace backward to the columns and other inputs that contribute to its value. Produce an evidence-backed machine-readable trace and a readable Markdown report for the requested column.

The tool must be generic. Do not encode a particular database, schema, object, column, source system, or stopping point in parser or traversal logic. A caller chooses the target and may choose a database or object boundary at which traversal stops. Design the model so the same intermediate lineage can support reports for many columns. The first implementation only needs to process one requested column per invocation.

This milestone uses **local SQL files only**. Do not connect to a database or execute scanned SQL. Keep the design open to a later adapter for local SSIS package files, but do not implement SSIS parsing now.

## Sharing boundary

Use only a sanitized copy of the Python project and invented SQL fixtures in the external LLM session. Do not provide actual SQL repositories, internal documentation, run artifacts, logs, repository URLs, or generated traces. If the code copy has not been checked and sanitized, request that step before implementation. Real-source acceptance testing will be performed later in the protected environment.

## Existing code

Start by inspecting the latest **sanitized** project copy supplied for the task. The project already discovers SQL files, indexes and parses statements, produces table lineage, and has an optional column definition artifact with ordered schemas, writer expressions, mapping methods, statuses, and statement evidence. Reuse those components where they are reliable. A definition or a table-level edge alone does not prove a source-to-target column mapping. Check binding and mapping status before traversing an edge.

Preserve the existing table-level CLI and output behavior. Version or separate new column output so existing consumers remain compatible. Follow the repository's own development and test instructions. Leave changes uncommitted for review.

## Public reference project

Review the public [SSIS Lineage Utility](https://github.com/okutue/SSIS-Project-Documentation) for reusable ideas. Its column graph and tracer support a selected column, upstream and downstream traversal, intermediate nodes, multi-step mappings, expression and filter fields, and report exports. Its SSIS parser may inform the later package adapter. Use it as a design reference, not as a correctness oracle or a dependency that requires a live database.

Check its assumptions before adapting them. In particular, column identity must include database context when known, and a wildcard edge must not be treated as a proven same-name column mapping without a reliable ordered schema. Preserve distinct value contributors, row-selection context, evidence, and uncertainty even if the reference graph stores these more simply. The public project's local desktop, live SQL enrichment, and extensive export formats are outside this milestone.

## Required behavior

1. Accept a target object and column and an optional caller-selected stopping boundary. The same code must work for unrelated columns and databases.
2. Follow renames and projections through SQL statements, stored-procedure writes, views, CTEs, derived tables, and temporary tables where the source provides enough evidence. Handle explicit target lists and positional or wildcard projections only when column order can be established reliably.
3. Preserve **all value contributors**. Expressions such as `CASE`, `COALESCE`, arithmetic, casts, and function calls can depend on multiple columns, constants, or parameters. Keep conditions and precedence attached to the appropriate contributors.
4. Record filters, join predicates, and join keys that affect which rows contribute as **row-selection dependencies**, distinct from columns that compute the target value.
5. Keep alternative branches and multiple writers distinct. Respect scope for temporary objects and CTEs. Where overwrite order or runtime branch selection cannot be established, show the ambiguity.
6. Treat dynamic SQL, missing definitions, unresolved aliases, unsupported statements, and ambiguous identities as explicit gaps. Do not infer a mapping from similar names or from an object-level dependency.
7. Attach file, line, statement, and relevant SQL-expression evidence to each supported step. Distinguish verified mappings from partial, inferred, ambiguous, and unresolved ones. A successful command exit must not imply that the lineage is complete.
8. Terminate recursion safely on cycles, reuse parsed source across traces, and generate deterministic output.

## Outputs

For the first release, produce **one Markdown page and one JSON file per requested column**. The Markdown page is the primary review output. Put a concise summary at the top and the detailed trace below it on the same page. CSV, interactive HTML, and other exports are outside this milestone.

Use this page structure, adapting the exact headings if needed for clarity:

1. **Identity and result:** fully qualified target column, scan scope, requested stopping boundary, run or source revision, and completeness status.
2. **Short answer:** plain-English description of the verified value sources, conditions, and where the trace stops. State material uncertainty here rather than hiding it in the details.
3. **Value lineage overview:** a compact branching view showing every value-contributing path and major rename or transformation. A conditional expression must show all alternatives; do not reduce it to one arrow.
4. **Row-selection context:** filters and join conditions that determine which rows contribute, explicitly separated from value inputs.
5. **Detailed steps:** an ordered table or sections for each mapping, including target and source column identities, expression or operation, branch condition, status, and evidence. When a step has multiple inputs, show each input and its role.
6. **Gaps and next evidence:** ambiguous writers, unsupported or dynamic statements, missing definitions, and the exact point where proof ends. Include a link or path to the companion JSON.

Evidence in the Markdown page should be a **file and line reference plus a short SQL excerpt** sufficient to recognize the mapping. Keep full indexed statements and structured evidence in JSON when available. Do not fill the page with whole procedures. Make the page regenerable from structured output rather than a manually maintained source of truth.

The companion JSON should be versioned and contain the complete trace graph: target, boundary, nodes, mapping edges, expressions, value-input roles, row-selection dependencies, branch conditions, evidence references, statuses, gaps, and completeness summary. Use stable column identities that include object context. Keep columns with the same name in different objects or databases distinct. A stopping boundary means the requested trace ended there; it does not claim that the boundary columns are ultimate source-system fields.

## Synthetic acceptance cases

Use invented SQL fixtures with neutral names. At minimum, verify that the same CLI and report generator can:

1. Trace `demo.output.result_value` through a rename and a temporary table to `demo.source.input_a`.
2. Trace a `CASE` expression that chooses between `demo.source.input_a` and `demo.source.input_b`, showing both value contributors and the condition.
3. Show a filter and join key as row-selection context without mislabeling them as value contributors.
4. Resolve a positional insert only with a known ordered schema; report a specific gap when the schema is absent.
5. Keep two unrelated target columns, multiple branches, and identically named columns in different objects separate.
6. Report an unresolved dynamic statement or ambiguous writer without inventing a complete path.
7. Stop at a caller-specified database boundary and show that a different boundary can be supplied without changing code.
8. Confirm the existing table-level CLI still behaves as documented.

The implementation may use different CLI option names and internal structures. Document the interface and make the tests exercise it. Do not add a special case for the fixture names.

## Handoff for review

Provide the changed-file summary, commands and test results, generated synthetic JSON and Markdown examples, known limitations, and any unproven edges. Explain how the tool will later ingest SSIS package and connection-manager metadata through the same column model. The implementation will be independently tested against additional source files after review.
