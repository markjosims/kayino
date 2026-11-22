import json
from pathlib import Path

import pytest

from kayino.core import apply_mappings, apply_prebuilt, apply_step
from kayino.models import MappingNorm, PrebuiltNorm
from kayino.prebuilt import PREBUILT_NORMS

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

    map_step = MappingNorm(name="test", data=mappings)
    result = apply_step(text, map_step)
    assert result == expected


@pytest.mark.parametrize("prebuilt_test", PREBUILTS)
def test_prebuilt(prebuilt_test):
    text = prebuilt_test["text"]
    expected = prebuilt_test["expected"]
    prebuilt_name = prebuilt_test["name"]

    result = apply_prebuilt(text, prebuilt_name)
    assert result == expected

    prebuilt_step = PrebuiltNorm(name=prebuilt_name)
    result = apply_step(text, prebuilt_step)
    assert result == expected

    prebuilt_step = PREBUILT_NORMS[prebuilt_name]
    result = apply_step(text, prebuilt_step)
    assert result == expected
