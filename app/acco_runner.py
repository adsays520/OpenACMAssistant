"""Execute only through guard.py; the full pipeline may write only its own run."""
import json
from pathlib import Path
import sys

import assistant
from acco import Runner


if __name__ == "__main__":
    folder = Path(sys.argv[1]).resolve(strict=True)
    config = json.loads((folder / "config.json").read_text())
    Runner(assistant, folder, config).run()
