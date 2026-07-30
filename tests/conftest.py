"""Pytest configuration for the test suite.

Puts the add-on directory on sys.path so `import logic`, `import events` and
`import radar` resolve when tests run from the repository root. Each test file
also carries the same shim inline, so the standalone `python3 tests/test_x.py`
runners work without pytest.

Place this file in tests/ alongside the test modules.
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))