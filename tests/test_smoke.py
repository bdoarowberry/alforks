import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from table_lineage.cli import main


FIXTURE = Path(__file__).parents[1] / "fixtures" / "basic"


def test_existing_table_cli_with_synthetic_source(tmp_path):
    assert main([
        "--target", "demo.final_result",
        "--source-root", str(FIXTURE),
        "--output", str(tmp_path),
        "--no-progress",
    ]) == 0

    result = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert result["target"] == "demo.final_result"
    assert (tmp_path / "lineage.mmd").exists()
    assert (tmp_path / "lineage.txt").exists()
