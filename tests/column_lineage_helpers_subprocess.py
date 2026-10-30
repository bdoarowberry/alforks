"""Run the column CLI in a child process with worker processes enabled."""
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parents[1] / "src"


def run_with_workers(root: Path, out: Path) -> subprocess.CompletedProcess:
    args = ["--column", "demo.output.parsed_value", "--source-root", str(root), "--output", str(out),
            "--no-progress", "--jobs", "2"]
    return subprocess.run([sys.executable, "-m", "column_lineage", *args], capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONPATH": str(SRC)}, timeout=300)
