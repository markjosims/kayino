import json
from pathlib import Path

import pytest

from kayino.core import apply_mappings

MAPPING_FILE = Path(__file__).parent / "mapping_tests.jsonl"
MAPPINGS = [json.loads(line) for line in MAPPING_FILE.read_bytes().splitlines()]


@pytest.mark.parametrize("mapping_test", MAPPINGS)
def test_no_transitivity(mapping_test):
    text = mapping_test["text"]
    expected = mapping_test["expected"]
    mappings = mapping_test["mappings"]
    result = apply_mappings(text, mappings)

    assert result == expected
