"""Source checkout convenience entry point; the packaged builder is also available with -m."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iatest.mobile.build import main

if __name__ == "__main__":
    main()
