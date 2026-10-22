"""Source and PyInstaller entry point for Plague's Broadcast Station."""
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from gnb_player.app import main

if __name__ == "__main__":
    raise SystemExit(main())
