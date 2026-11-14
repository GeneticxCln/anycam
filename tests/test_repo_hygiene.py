"""Rules from CONTRIBUTING.md that a file name can break."""
import re
from pathlib import Path

TESTS = Path(__file__).parent


def test_no_test_file_is_named_after_a_date_an_audit_or_a_phase():
    pattern = re.compile(r"20\d\d|phase\d|audit|review_fixes|housekeeping|cleanups")
    bad = sorted(p.name for p in TESTS.glob("test_*.py") if pattern.search(p.name))
    assert not bad, f"name test files after the feature they cover: {bad}"
