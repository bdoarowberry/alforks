"""Robustness (review 12): input shapes that crashed, hung or silently lost files. All invented."""
import json
import time

from column_helpers import FIXTURES, cli_args, run_cli, trace

DB = ("--default-database", "demo")


def write(path, text, encoding="utf-8", newline=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, newline=newline) if newline is not None else path.write_text(text, encoding=encoding)


def test_cr_only_line_endings_with_a_comment_do_not_hide_the_file(tmp_path):
    sql = tmp_path / "sql"
    write(sql / "cr.sql", "-- loader\rCREATE TABLE dbo.S (v int);\rCREATE TABLE dbo.T (v int);\rGO\r"
                          "CREATE PROCEDURE dbo.load AS INSERT INTO dbo.T (v) SELECT v FROM dbo.S;\rGO\r", newline="")
    t = trace(sql, "demo.dbo.T.v", tmp_path / "out", *DB)
    assert t.complete and t.data["scan"]["statements"] >= 3
    assert t.only_step_for(t.target_id)["evidence"][0]["start_line"] > 1


def test_long_identifiers_do_not_crash_the_run(tmp_path):
    table, column = "t" + "x" * 127, "c" + "y" * 127
    sql = tmp_path / "sql"
    write(sql / "long.sql", f"CREATE TABLE dbo.{table} ({column} int);\nCREATE TABLE dbo.Src (v int);\nGO\n"
                            f"CREATE PROCEDURE dbo.load AS INSERT INTO dbo.{table} ({column}) SELECT v FROM dbo.Src;\nGO\n")
    out = tmp_path / "out"
    assert run_cli(cli_args(sql, [f"demo.dbo.{table}.{column}", "demo.dbo.Src.v"], out, *DB)) == 0
    names = sorted(p.name for p in out.glob("*.lineage.json"))
    assert len(names) == 2 and all(len(n) < 150 for n in names)
    assert (out / "routes-index.json").exists() and (out / "scan-diagnostics.json").exists()


def test_distinct_columns_never_share_an_output_file():
    from column_lineage.cli import stem
    assert stem("[demo].[demo].[out long].[v space]") == stem("demo.demo.[out long].[v space]")   # same column
    assert stem("demo.demo.out.v") == stem("DEMO.DEMO.OUT.V")
    assert stem("demo.dbo.t.[a b]") != stem("demo.dbo.t.a_b")
    assert stem("demo.dbo.t.[col-1]") != stem("demo.dbo.t.col_1")
    assert len(stem("demo.dbo." + "x" * 200 + ".c")) <= 130


def test_bad_job_scripts_path_and_bad_column_specs_are_errors(tmp_path):
    root = FIXTURES / "earlier_state"
    assert run_cli(cli_args(root, "demo.dbo.Target.col_a", tmp_path / "a", *DB,
                            "--job-scripts", str(tmp_path / "missing"))) == 2
    assert run_cli(cli_args(root, "this is not a column", tmp_path / "b", *DB)) == 2
    assert run_cli(cli_args(root, "demo.dbo.Target.col_a;", tmp_path / "c", *DB)) == 2


def test_overlapping_source_roots_index_a_file_once(tmp_path):
    root = FIXTURES / "update_alias"
    files = sorted(root.glob("*.sql"))
    t = trace(root, "demo.output.result_value", tmp_path, "--source-root", str(files[0]))
    assert t.data["scan"]["files"] == len(files) and t.data["scan"]["duplicate_root_files"] == 1
    assert "ambiguous_writer_order" not in t.gap_kinds


def test_a_stray_nul_byte_does_not_hide_the_file(tmp_path):
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "nul.sql").write_bytes(b"-- marker \x00 in a comment\nCREATE TABLE dbo.S (v int);\nCREATE TABLE dbo.T (v int);\nGO\n"
                                  b"CREATE PROCEDURE dbo.load AS INSERT INTO dbo.T (v) SELECT v FROM dbo.S;\nGO\n")
    t = trace(sql, "demo.dbo.T.v", tmp_path / "out", *DB)
    assert t.complete and "utf-8-nul-stripped" in t.data["scan"]["file_encodings"]


def test_deep_nesting_loses_one_statement_not_the_file(tmp_path):
    sql = tmp_path / "sql"
    deep = "v"
    for i in range(300):
        deep = f"CASE WHEN v > {i} THEN {deep} ELSE 0 END"
    write(sql / "deep.sql", "CREATE TABLE dbo.S (v int);\nCREATE TABLE dbo.T (v int);\nCREATE TABLE dbo.U (v int);\nGO\n"
                            f"CREATE PROCEDURE dbo.deep AS INSERT INTO dbo.U (v) SELECT {deep} FROM dbo.S;\nGO\n"
                            "CREATE PROCEDURE dbo.load AS INSERT INTO dbo.T (v) SELECT v FROM dbo.S;\nGO\n")
    t = trace(sql, "demo.dbo.T.v", tmp_path / "out", *DB)
    assert t.complete   # the deep statement did not take the CREATE TABLEs down with it
    assert t.data["scan"]["file_errors"] == 0


def test_one_large_file_is_not_quadratic(tmp_path):
    sql = tmp_path / "sql"
    n = 1500
    body = ["CREATE TABLE dbo.S (v int);", "CREATE TABLE dbo.T (v int);", "GO"]
    for i in range(n):
        body += [f"CREATE PROCEDURE dbo.p{i} AS UPDATE dbo.T SET v = v + {i} WHERE v > {i};", "GO"]
    write(sql / "all_procs.sql", "\n".join(body) + "\n")
    started = time.monotonic()
    assert run_cli(cli_args(sql, "demo.dbo.T.v", tmp_path / "out", *DB, "--no-overview", "--jobs", "1")) == 0
    assert time.monotonic() - started < 20
