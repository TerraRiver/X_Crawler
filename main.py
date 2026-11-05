"""Backward-compatible entry point: `uv run python main.py [options]` (see `--help`)."""

import sys

from x_crawler.cli import main

if __name__ == "__main__":
    sys.exit(main())
