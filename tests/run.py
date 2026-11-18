"""Run the named test modules and leave without the interpreter's teardown.

The suite loads Qt, pyqtgraph and pylon's SWIG modules. On some machines, the GitHub macOS
runner among them, tearing those down at interpreter exit crashes the process after every
test has passed, which make reads as a failure. The result is decided here and reported, the
output flushed, and the process ends with os._exit, which skips the teardown.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # the project root, as -m unittest sees it


def main(argv: list[str]) -> int:
    modules = [name for name in argv if name]
    if not modules:
        print("usage: run.py tests.module [tests.module ...]", file=sys.stderr)
        return 2
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in modules)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
