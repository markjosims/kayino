import json
from pathlib import Path

import pytest

from kayino.core import apply_mappings, apply_prebuilt, apply_step
from kayino.models import MappingNorm, MappingRow, PrebuiltNorm
from kayino.prebuilt import PREBUILT_NORMS
from kayino.structs import change, connect, insert

MAPPING_FILE = Path(__file__).parent / "test_cases" / "mapping_tests.jsonl"
MAPPINGS = [json.loads(line) for line in MAPPING_FILE.read_bytes().splitlines()]

PREBUILT_FILE = Path(__file__).parent / "test_cases" / "prebuilt_tests.jsonl"
PREBUILTS = [json.loads(line) for line in PREBUILT_FILE.read_bytes().splitlines()]


@pytest.mark.parametrize("mapping_test", MAPPINGS)
def test_no_transitivity(mapping_test):
    text = mapping_test["text"]
    expected = mapping_test["expected"]
    mappings = mapping_test["mappings"]

    result = apply_mappings(text, mappings)
    assert result == expected

    db = connect()
    with change(db):
        insert(db, MappingNorm.__name__, name="test")
        for intab, outtab in mappings.items():
            insert(db, MappingRow.__name__, norm="test", intab=intab, outtab=outtab)
    result = apply_step(text, MappingNorm(name="test"), db)
    assert result == expected


@pytest.mark.parametrize("prebuilt_test", PREBUILTS)
def test_prebuilt(prebuilt_test):
    text = prebuilt_test["text"]
    expected = prebuilt_test["expected"]
    prebuilt_name = prebuilt_test["name"]

    result = apply_prebuilt(text, prebuilt_name)
    assert result == expected

    registry = connect()
    prebuilt_step = PrebuiltNorm(name=prebuilt_name)
    result = apply_step(text, prebuilt_step, registry)
    assert result == expected

    prebuilt_step = PREBUILT_NORMS[prebuilt_name]
    result = apply_step(text, prebuilt_step, registry)
    assert result == expected
