# Sanitized column lineage handoff

This directory is a separate working copy for an external implementation LLM. It contains the sanitized Python package, its dependency metadata, the build brief, invented SQL fixtures, and a copy of the public reference project. It has no Git history, internal repository URL, real SQL, or original tests.

Read [BUILD_BRIEF.md](BUILD_BRIEF.md) first. The public [SSIS Lineage Utility](reference/SSIS-Project-Documentation/README.md) is included under `reference/` as a design reference; it is not a dependency. Its original [MIT license](reference/SSIS-Project-Documentation/LICENSE) is included.

## Contents

- `src/table_lineage/`: source code from the newer project revision, with repository-specific directory labels changed to the synthetic `sql_root_a` and `sql_root_b`.
- `fixtures/basic/`: invented SQL for testing a conditional value, rename, temporary table, join, and filter.
- `tests/test_smoke.py`: a small regression test for the existing table-level CLI.
- `BUILD_BRIEF.md`: the requested generic column-lineage behavior and output.
- `reference/SSIS-Project-Documentation/`: tracked files from the public project, with no local Git metadata or ignored files.

The original project contains additional tests and fixtures that were deliberately omitted because they contain internal names. The synthetic test suite is **not equivalent to the original suite**. Before applying the completed implementation back to the original project, compare changes carefully and run its full tests in the protected environment. The synthetic directory labels are handoff substitutions, not production configuration.

## Local use

From this directory, in a suitable isolated Python environment:

```powershell
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest -p no:cacheprovider
$env:PYTHONPATH = "src"
python -m table_lineage.cli --target demo.final_result --source-root fixtures/basic --output output
```

Do not add real SQL files, run artifacts, connection strings, logs, or internal documentation to this handoff. Use synthetic examples for development and sharing.
