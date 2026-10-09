"""Run offline regressions with an owned temporary data directory."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


def main():
    root = Path(__file__).resolve().parent
    previous = os.environ.get('PI_SCRATCH_DIR')
    try:
        with tempfile.TemporaryDirectory(prefix='ptskit-tests-', dir=previous) as scratch:
            os.environ['PI_SCRATCH_DIR'] = scratch
            with contextlib.redirect_stdout(sys.stderr):
                suite = unittest.defaultTestLoader.discover(str(root), pattern='test_seedkeep*.py', top_level_dir=str(root))
                result = unittest.TextTestRunner(verbosity=1).run(suite)
            print(json.dumps({'tests_run': result.testsRun, 'skipped': len(result.skipped),
                              'failures': len(result.failures), 'errors': len(result.errors)}))
            return 0 if result.wasSuccessful() else 1
    finally:
        if previous is None:
            os.environ.pop('PI_SCRATCH_DIR', None)
        else:
            os.environ['PI_SCRATCH_DIR'] = previous


if __name__ == '__main__':
    sys.exit(main())
