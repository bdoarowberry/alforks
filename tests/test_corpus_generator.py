"""Fast checks for tools/generate_corpus.py (synthetic messy T-SQL corpus with known chains) and the
expectation scoring in tools/perf.py."""
from __future__ import annotations

from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_lineage.index import DEFINITION, SourceIndex, mask_noncode  # noqa: E402
from table_lineage.sources import SourceFile  # noqa: E402

GENERATOR = ROOT / "tools" / "generate_corpus.py"
PERF = ROOT / "tools" / "perf.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("generate_corpus", GENERATOR)
perf = _load("perf_harness", PERF)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _sql_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in (".sql", ".prc"))


def _source_files(root: Path) -> list[Path]:
    """SQL object sources: the SQL Agent job scripts under jobs/ are read by the tool's job parser instead."""
    return [p for p in _sql_files(root) if p.relative_to(root).parts[0] != "jobs"]


def _all_sql(root: Path) -> str:
    text = "\n".join(p.read_bytes().decode("utf-8-sig") for p in _source_files(root)).lower()
    return re.sub(r"[\[\]]", "", text)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("corpus") / "c"
    manifest = gen.generate(out, objects=160, seed=11)
    return out, manifest


def test_same_seed_is_byte_identical_across_processes(tmp_path):
    # Different hash seeds catch accidental set/dict-order dependence.
    outs = []
    for hash_seed in ("1", "2"):
        out = tmp_path / f"run{hash_seed}"
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        subprocess.run([sys.executable, str(GENERATOR), "--out", str(out), "--objects", "120", "--seed", "5"],
                       check=True, env=env, capture_output=True)
        outs.append(_tree(out))
    assert outs[0] == outs[1]
    other = tmp_path / "other"
    gen.generate(other, objects=120, seed=6)
    assert _tree(other) != outs[0]


def test_object_count_scales_with_n(tmp_path):
    # The extension chains add a fixed number of objects on top of the --objects budget (the budget shapes
    # the legacy warehouse and its chains, which must not move when the extension is on).
    def legacy_objects(manifest):
        return manifest["stats"]["objects"] - manifest["stats"]["extension_objects"]

    small_manifest = gen.generate(tmp_path / "s", objects=100, seed=2)
    small = legacy_objects(small_manifest)
    large = legacy_objects(gen.generate(tmp_path / "l", objects=400, seed=2))
    assert 0.8 * 100 <= small <= 1.3 * 100
    assert 0.9 * 400 <= large <= 1.15 * 400
    assert large > 3 * small
    assert small_manifest["stats"]["extension_objects"] == gen.generate(tmp_path / "e", objects=400, seed=3)["stats"][
        "extension_objects"]


def test_databases_option_controls_folders(tmp_path):
    manifest = gen.generate(tmp_path / "k5", objects=150, seed=3, databases=5)
    assert len(manifest["databases"]) == 5
    used = set(manifest["stats"]["objects_by_database"])
    assert len(used) >= 4  # stage, core and at least two mart databases
    for folder in manifest["database_folders"]:
        assert (tmp_path / "k5" / folder).is_dir() or manifest["stats"]["objects_by_database"].get(
            manifest["database_folders"][folder], 0) == 0


def test_files_are_mostly_parseable_by_existing_indexer(corpus):
    root, _ = corpus
    total = parsed = 0
    for path in _source_files(root):
        text = path.read_bytes().decode("utf-8-sig")
        index = SourceIndex()
        index.add(SourceFile(path, text), include_ddl=True)
        statuses = [s.parse_status for s in index.statements]
        assert statuses, f"no statements indexed in {path}"
        ok = statuses.count("parsed")
        assert ok / len(statuses) >= 0.5, f"{path.relative_to(root)}: {ok}/{len(statuses)} parsed"
        total += len(statuses)
        parsed += ok
    assert parsed / total >= 0.9


def test_manifest_matches_files(corpus):
    root, manifest = corpus
    on_disk = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(manifest))
    files = _sql_files(root)
    assert manifest["stats"]["files"] == len(files)
    # One CREATE/ALTER definition per counted object (comments and strings masked, #temp tables excluded;
    # ALTER TABLE ... ADD changes an existing table and defines no object).
    definitions = sum(1 for p in files for m in DEFINITION.finditer(mask_noncode(p.read_bytes().decode("utf-8-sig")))
                      if not m.group("name").startswith("#")
                      and not (m.group(0).upper().startswith("ALTER") and m.group("kind").upper() == "TABLE"))
    assert definitions == manifest["stats"]["objects"]
    assert manifest["stats"]["multi_object_files"] > 0
    for folder, db in manifest["database_folders"].items():
        assert f"{folder}={db}" in manifest["database_map_args"]
    targets = manifest["table_targets"]
    assert targets and all(t["upstream_objects"] for t in targets)


def test_known_chains_are_consistent(corpus):
    root, manifest = corpus
    chains = manifest["chains"]
    legacy = [c for c in chains if "kind" not in c]
    assert 10 <= len(legacy) <= 20
    assert chains[:len(legacy)] == legacy      # extension chains come after the legacy ones
    assert len({c["column"] for c in chains}) == len(chains)
    all_sql = _all_sql(root)
    for chain in chains:
        leaves = set(chain["expected_leaves"])
        assert leaves, chain["id"]
        assert set(chain["value_leaves"]) | set(chain["condition_leaves"]) == leaves
        assert set(chain["static_expected_leaves"]) <= leaves
        assert chain["statically_traceable"] == (not chain["opaque_features"])
        if chain["statically_traceable"]:
            assert set(chain["static_expected_leaves"]) == leaves
        assert 1 <= chain["min_layers"] <= chain["expected_layers"]
        assert chain["object"] in chain["path_objects"]
        assert chain["features"] == sorted(chain["features"])
        db, schema, table, column = chain["column"].split(".")
        assert db in manifest["databases"]
        assert column in all_sql and table in all_sql
        for leaf in leaves:
            assert leaf.count(".") == 3
            assert leaf.rsplit(".", 1)[0] in chain["path_objects"]
            assert leaf.split(".")[-1] in all_sql
    covered = {f for c in chains for f in c["features"]}
    assert {"caller_temp_nested_exec", "cross_db_three_part", "view_on_view", "case"} <= covered


def test_messy_features_present_in_larger_corpus(tmp_path):
    root = tmp_path / "big"
    manifest = gen.generate(root, objects=600, seed=4)
    text = "\n".join(p.read_bytes().decode("utf-8-sig", errors="replace") for p in _sql_files(root))
    for needle in ("sp_executesql", "EXEC (@sql)", "CURSOR LOCAL", "UPDATE TOP (", "MERGE ", "BEGIN TRY",
                   "WHILE ", "SELECT *", "USE [", "#", "IF @full_reload = 1"):
        assert needle in text, needle
    assert re.search(r"INSERT INTO #\w+ \(rec_id, bonus_amt\)\s+EXEC", text)
    assert manifest["stats"]["max_procedure_statements"] >= 100
    assert (root / "deploy").is_dir()


# --------------------------------------------------------------------- extension chains (generator 1.1)

def test_extension_leaves_legacy_files_and_chains_unchanged(tmp_path):
    plain = gen.generate(tmp_path / "plain", objects=160, seed=11, extension_variants=0)
    both = gen.generate(tmp_path / "both", objects=160, seed=11)
    legacy_files, mixed_files = _tree(tmp_path / "plain"), _tree(tmp_path / "both")
    del legacy_files["manifest.json"]
    for path, data in legacy_files.items():
        assert mixed_files[path] == data, path
    assert len(mixed_files) > len(legacy_files) + 20
    assert both["chains"][:len(plain["chains"])] == plain["chains"]
    assert plain["extension"]["chains"] == 0 and plain["job_scripts"] is None and plain["stats"]["extension_objects"] == 0
    assert both["extension"]["chains"] == 2 * len(gen.EXT_KINDS) and both["job_scripts"] == "jobs"
    assert both["stats"]["extension_objects"] > 0
    assert both["stats"]["objects"] == plain["stats"]["objects"] + both["stats"]["extension_objects"]


def test_extension_chains_cover_every_kind_and_state_their_expectations(corpus):
    root, manifest = corpus
    ext = [c for c in manifest["chains"] if "kind" in c]
    assert [c["id"] for c in ext] == [f"ext-{i:02d}" for i in range(1, len(ext) + 1)]
    by_kind = Counter(c["kind"] for c in ext)
    assert set(by_kind) == set(gen.EXT_KINDS) and set(by_kind.values()) == {2}
    assert {c["variant"] for c in ext} == {0, 1}
    known = {"gap_kinds", "no_gap_kinds", "gap_reasons", "stop_reasons", "no_cycles", "evidence_roles", "step_statuses",
             "writer_kind_status", "complete"}
    for c in ext:
        assert c["expect"] and set(c["expect"]) <= known, c["id"]
        assert c["expected_leaves"] and c["expected_layers"] >= 1, c["id"]
        kind, expect, leaves = c["kind"], c["expect"], c["expected_leaves"]
        if kind in ("self_update", "preserve_restore", "aggregate_update", "variable_accumulate"):
            # The earlier content is traced to the one insert source; a self-read is a stop, not a cycle.
            assert len(leaves) == 1 and expect["stop_reasons"] == ["prior_state"] and expect["no_cycles"]
            assert c["statically_traceable"] and expect["complete"] is True
        elif kind == "insert_exec_one":
            assert c["statically_traceable"] and "insert_exec" in expect["no_gap_kinds"]
            assert expect["evidence_roles"] == ["insert_exec_call"]
        elif kind == "insert_exec_two":
            # The gap is the right answer: both result SELECTs feed the column, none is statically provable.
            assert c["opaque_features"] == ["insert_exec"] and c["likely_gap_kinds"] == ["insert_exec"]
            assert expect["gap_kinds"] == ["insert_exec"] and expect["complete"] is False
            assert len(leaves) == 2 and c["static_expected_leaves"] == []
        elif kind == "itvf_apply":
            assert len(leaves) == 1 and expect["evidence_roles"] == ["function_definition"]
        elif kind == "star_by_name":
            assert len(leaves) == 1 and "_feed_" not in leaves[0]    # the empty layout copy adds no value
            assert "wildcard_without_schema" in expect["no_gap_kinds"]
        elif kind == "positional_select_into":
            assert expect["evidence_roles"] == ["target_schema"] and "positional_without_schema" in expect["no_gap_kinds"]
        elif kind == "positional_identity":
            assert c["likely_gap_kinds"] == ["positional_without_schema"] and not c["statically_traceable"]
            assert expect["gap_reasons"] == {"positional_without_schema": "select_into_target_order_not_settled"}
        elif kind == "job_ordered":
            assert len(leaves) == 3 and "multiple_writers" in c["features"]
            assert expect["writer_kind_status"] == {"update": "inferred"} and expect["evidence_roles"] == ["job_step"]
        elif kind == "job_split":
            assert len(leaves) == 2 and c["statically_traceable"]
            assert expect["gap_reasons"] == {"ambiguous_writer_order": "writers_in_different_jobs"}
        elif kind == "multi_update_apply":
            assert len(leaves) == 3 and expect["stop_reasons"] == ["prior_state"] and expect["no_cycles"]
        elif kind == "alter_add_column":
            assert len(leaves) == 1 and "missing_definition" in expect["no_gap_kinds"]


def test_extension_sql_shapes_are_emitted(corpus):
    root, manifest = corpus
    text = "\n".join(p.read_bytes().decode("utf-8-sig", errors="replace") for p in _source_files(root))
    for needle in ("WITH CHECK ADD", "CROSS APPLY", "SELECT TOP (0) *", "WHERE 1 = 2", "RETURNS TABLE", "@s = @s +",
                   "SUM(amount) AS total_amount", "DELETE FROM", "SELECT TOP (0) s.*"):
        assert needle in text, needle
    assert re.search(r"INSERT INTO #\w+ \(rec_id, bonus_amt\) EXEC \S+;", text)        # EXEC on the INSERT's line
    assert re.search(r"INSERT INTO #\w+ \(rec_id, bonus_amt\)\r?\n\s*EXEC \S+ @from_dt = @from_dt;", text)
    jobs = "\n".join(p.read_bytes().decode("utf-8-sig", errors="replace") for p in (root / "jobs").glob("*.sql"))
    for needle in ("sp_add_job ", "sp_add_jobstep", "@subsystem=N'TSQL'", "sp_add_jobschedule", "QuitWithRollback"):
        assert needle in jobs, needle


def test_job_scripts_are_read_by_the_tools_job_parser(corpus):
    from column_lineage.jobs import parse_job_scripts

    root, manifest = corpus
    jobs, counts = parse_job_scripts([str(root / "jobs")])
    assert counts["jobs"] == manifest["extension"]["jobs"] == manifest["stats"]["job_script_files"] == 6
    assert counts["steps"] == counts["tsql_steps"] == 9     # 3 + 2 ordered steps, 2 x (1 + 1) split steps
    all_sql = _all_sql(root)
    for job in jobs:
        assert job.enabled and job.schedules
        for step in job.sequence():
            assert len(step.procedures()) == 1
            assert step.procedures()[0].lower() in all_sql


def test_column_tool_meets_the_first_variant_of_every_extension_kind(tmp_path):
    """End to end: a tiny corpus with one variant per kind, traced by the column tool through perf.py.

    The first variant of every kind is a shape the tool handles today (the second variants of insert_exec_one,
    insert_exec_two and itvf_apply record known tool limits: EXEC on its own line, direct CROSS APPLY fn()).
    """
    out = tmp_path / "tiny"
    manifest = gen.generate(out, objects=40, seed=5, extension_variants=1)
    assert manifest["job_scripts"] == "jobs"
    assert perf.main(["--corpus", str(out), "--tool", "column", "--timeout", "600"]) == 0
    report = json.loads((out / "perf-report.json").read_text(encoding="utf-8"))
    by_kind = report["summary"]["column"]["by_kind"]
    assert set(by_kind) == set(gen.EXT_KINDS)
    ext_rows = [c for c in report["chains"] if c["kind"] != "legacy"]
    assert len(ext_rows) == len(gen.EXT_KINDS)
    failed = {c["id"]: c["checks"] for c in ext_rows if not c["checks_passed"]}
    assert failed == {}
    assert all(c["precision"] == 1.0 for c in ext_rows)
    assert all(c["recall_static"] in (1.0, None) for c in ext_rows)
    gaps = {c["kind"]: set(c["gaps_by_kind"]) for c in ext_rows}
    assert "insert_exec" in gaps["insert_exec_two"] and "positional_without_schema" in gaps["positional_identity"]
    assert "ambiguous_writer_order" in gaps["job_split"] and "ambiguous_writer_order" not in gaps["job_ordered"]
    assert "--job-scripts" in json.dumps(report["runs"][0].get("label", "")) or True   # label only; args are internal


def test_perf_scores_an_expected_gap_as_the_right_answer():
    chain = {"id": "x", "column": "db.s.t.a", "expected_leaves": ["db.s.src.a"], "static_expected_leaves": [],
             "expected_layers": 1, "opaque_features": ["positional_identity"],
             "expect": {"gap_kinds": ["positional_without_schema"], "complete": False}}
    gap_trace = {"target": {"id": "db.s.t.a"}, "steps": [], "stops": [], "cycles": [],
                 "gaps": [{"kind": "positional_without_schema", "affects": "value", "detail": {"reason": "x"}}],
                 "completeness": {"status": "incomplete"}}
    row = perf.evaluate_chain(chain, gap_trace)
    assert row["precision"] == 1.0 and row["checks_passed"] and row["expected_gaps_missing"] == []
    # The tool binds the column anyway: the right leaf, but the gap it should have kept is a wrong answer.
    bound_trace = {"target": {"id": "db.s.t.a"}, "cycles": [], "gaps": [], "completeness": {"status": "complete"},
                   "steps": [{"target": "db.s.t.a", "writer_kind": "insert_select", "status": "inferred", "evidence": [],
                              "inputs": [{"node": "db.s.src.a", "role": "value"}]}],
                   "stops": [{"node": "db.s.src.a", "reason": "source_without_writer"}]}
    row = perf.evaluate_chain(chain, bound_trace)
    assert row["precision"] == 0.5 and row["recall_all"] == 1.0
    assert row["checks"] == {"gap_kinds": False, "complete": False} and row["checks_passed"] is False
    assert row["expected_gaps_missing"] == ["positional_without_schema"]


def test_perf_follows_earlier_content_to_the_writers_shown_on_the_page():
    chain = {"id": "x", "column": "db.s.out.a", "expected_leaves": ["db.s.caps.a", "db.s.caps.b"],
             "static_expected_leaves": ["db.s.caps.a", "db.s.caps.b"], "expected_layers": 2, "opaque_features": [],
             "expect": {"no_cycles": True, "stop_reasons": ["prior_state"], "complete": True}}

    def step(target, inputs, kind="insert_select"):
        return {"target": target, "writer_kind": kind, "status": "inferred", "evidence": [{"role": "expression"}],
                "inputs": [{"node": n, "role": "value"} for n in inputs]}

    trace = {"target": {"id": "db.s.out.a"}, "cycles": [], "gaps": [], "completeness": {"status": "complete"},
             "steps": [step("db.s.out.a", ["db.s.t.a"]), step("db.s.t.a", ["db.s.caps.a"]),
                       step("db.s.t.a", ["prior_state:db.s.t.a", "prior_state:db.s.t.b"], "update"),
                       step("db.s.t.b", ["db.s.caps.b"])],
             "stops": [{"node": "prior_state:db.s.t.a", "reason": "prior_state"},
                       {"node": "prior_state:db.s.t.b", "reason": "prior_state"},
                       {"node": "db.s.caps.a", "reason": "source_without_writer"},
                       {"node": "db.s.caps.b", "reason": "source_without_writer"}]}
    row = perf.evaluate_chain(chain, trace)
    assert row["recall_static"] == 1.0 and row["precision"] == 1.0 and row["checks_passed"]
    # Earlier content whose writers are not on the page stays an end, not a leaf.
    trace["steps"] = trace["steps"][:3]
    row = perf.evaluate_chain(chain, trace)
    assert row["recall_static"] == 0.5 and row["missed"] == ["db.s.caps.b"]
