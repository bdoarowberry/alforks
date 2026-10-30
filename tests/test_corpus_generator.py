"""Fast checks for tools/generate_corpus.py (synthetic messy T-SQL corpus with known chains)."""
from __future__ import annotations

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


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_corpus", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_corpus"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _sql_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in (".sql", ".prc"))


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
    small = gen.generate(tmp_path / "s", objects=100, seed=2)["stats"]["objects"]
    large = gen.generate(tmp_path / "l", objects=400, seed=2)["stats"]["objects"]
    assert 0.8 * 100 <= small <= 1.3 * 100
    assert 0.9 * 400 <= large <= 1.15 * 400
    assert large > 3 * small


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
    for path in _sql_files(root):
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
    # One CREATE/ALTER definition per counted object (comments and strings masked, #temp tables excluded).
    definitions = sum(1 for p in files for m in DEFINITION.finditer(mask_noncode(p.read_bytes().decode("utf-8-sig")))
                      if not m.group("name").startswith("#"))
    assert definitions == manifest["stats"]["objects"]
    assert manifest["stats"]["multi_object_files"] > 0
    for folder, db in manifest["database_folders"].items():
        assert f"{folder}={db}" in manifest["database_map_args"]
    targets = manifest["table_targets"]
    assert targets and all(t["upstream_objects"] for t in targets)


def test_known_chains_are_consistent(corpus):
    root, manifest = corpus
    chains = manifest["chains"]
    assert 10 <= len(chains) <= 20
    assert len({c["column"] for c in chains}) == len(chains)
    all_sql = "\n".join(p.read_bytes().decode("utf-8-sig") for p in _sql_files(root)).lower()
    all_sql = re.sub(r"[\[\]]", "", all_sql)
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
