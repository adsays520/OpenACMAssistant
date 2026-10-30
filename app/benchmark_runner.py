"""Only launched inside guard.py, with all outputs under one experiment folder."""
import json
from pathlib import Path
import sys

import assistant
from benchmark import Benchmark


if __name__ == "__main__":
    folder = Path(sys.argv[1]).resolve(strict=True)
    runner = Benchmark(assistant, folder, json.loads((folder / "config.json").read_text()))
    runner.save()
    try:
        runner.run()
    except Exception as exc:
        path = folder / "result.json"
        state = json.loads(path.read_text()) if path.exists() else {"kind": "search_comparison"}
        state.update(status="failed", error=str(exc), stop_reason="initialization_error")
        assistant.write_json(path, state)
        raise
