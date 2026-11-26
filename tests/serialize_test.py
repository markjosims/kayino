import json

import pytest
from attrs import asdict
from cattrs import structure
from frozendict import frozendict

from kayino.models import (
    CdrewriteArgs,
    CdrewriteNorm,
    MappingNorm,
    NormalizationSpec,
    NormStep,
    Pattern,
    PrebuiltNorm,
    Symbol,
    Text,
    Token,
)
from kayino.repl.models import ReplState
from kayino.structs import init_registry


@pytest.mark.parametrize(
    "value, struct_type",
    [
        (Token(string="a"), Token),
        (Pattern(string="a", value="a+"), Pattern),
        (PrebuiltNorm(name="lowercase"), PrebuiltNorm),
        (MappingNorm(name="replace", data={"a": "b"}), MappingNorm),
        (
            CdrewriteNorm(
                name="rewrite",
                data=CdrewriteArgs(input_str="a", output_str="b"),
            ),
            CdrewriteNorm,
        ),
        (
            NormalizationSpec(
                name="english", steps=("lowercase",), licit_patterns=("[a-z]+",)
            ),
            NormalizationSpec,
        ),
        (Text(string="hello", source_type="file", source_value="input.txt"), Text),
        (
            ReplState(
                registry=init_registry(),
                texts={"0": "hello"},
                active_spec="english",
            ),
            ReplState,
        ),
    ],
)
def test_struct_json_round_trip(value, struct_type):
    json_string = json.dumps(asdict(value))

    restored = structure(json.loads(json_string), struct_type)

    assert restored == value


@pytest.mark.parametrize(
    "value, union_type",
    [
        (Token(string="a"), Symbol),
        (Pattern(string="a", value="a+"), Symbol),
        (PrebuiltNorm(name="lowercase"), NormStep),
        (MappingNorm(name="replace", data={"a": "b"}), NormStep),
        (
            CdrewriteNorm(
                name="rewrite",
                data=CdrewriteArgs(input_str="a", output_str="b"),
            ),
            NormStep,
        ),
    ],
)
def test_union_json_round_trip(value, union_type):
    json_string = json.dumps(asdict(value))

    restored = structure(json.loads(json_string), union_type)

    assert type(restored) is type(value)
    assert restored == value
