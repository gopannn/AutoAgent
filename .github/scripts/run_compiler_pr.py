"""Thin wrapper so the workflow can call the PR runner by path. See verification_compiler/ci.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from verification_compiler.ci import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
