"""Run the repository quality contract using only the Python standard library."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "packages/graph_runtime_langgraph/src/engine/graph_runtime_langgraph/quality.py"), run_name="__main__")
