#!/usr/bin/env python3
"""Allow: python -m harness  (same as unittest discover in project root)."""
import unittest
import sys

def main():
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir='.', pattern='test_*.py', top_level_dir='.')
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == '__main__':
    main()
