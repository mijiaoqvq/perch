#!/usr/bin/python3
"""Compatibility entry point for the existing systemd service."""
from pathlib import Path
import sys

installed = Path.home() / ".local/share/perch"
if not (Path(__file__).resolve().parent / "perch").is_dir():
    sys.path.insert(0, str(installed))
from perch.__main__ import main

if __name__ == "__main__":
    sys.argv.insert(1, "update")
    raise SystemExit(main())
