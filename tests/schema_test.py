"""
Tests for table declarations, the tagged-union converter and the
nested-view query helpers.
"""

import pytest
from attrs import frozen

from kayino.models import (
    CONVERTER,
    TABLES,
    CdrewriteNorm,
    GroupMember,
    MappingNorm,
    MappingRow,
    NormStep,
    Pattern,
    PrebuiltNorm,
    SpecStep,
    Symbol,
    Token,
    TokenGroup,
    table_for,
)
from kayino.schema import Table, check_tables, ref
from kayino.structs import (
    build_struct,
    get_structs,
    group_members,
    init_registry,
    mapping,
    next_position,
    spec_steps,
)


def test_declared_tables_are_consistent():
    assert check_tables(TABLES) == []


def test_check_tables_reports_bad_declarations():
    @frozen
    class Orphan:
        parent: str = ref("nowhere")
        other: str = ref("symbols", column="missing", kind="bogus")

    problems = check_tables(
        TABLES
        + (
            Table("orphans", Orphan, key=("id",)),
            Table("tokens_again", Token, key=("name",)),
        )
    )
    joined = "\n".join(problems)
    assert "unknown table 'nowhere'" in joined
    assert "unknown column symbols.missing" in joined
    assert "kind 'bogus'" in joined
    assert "key column 'id' missing" in joined
    assert "Token appears in both" in joined


@pytest.mark.parametrize(
    "union, struct",
    [
        (Symbol, Token(name="a")),
        (Symbol, TokenGroup(name="V")),
        (Symbol, Pattern(name="P", value="a+")),
        (NormStep, PrebuiltNorm(name="lowercase")),
        (NormStep, MappingNorm(name="m", description="d")),
        (NormStep, CdrewriteNorm(name="r", input_str="a", right_context="b")),
    ],
)
def test_tagged_union_round_trip(union, struct):
    data = CONVERTER.unstructure(struct)
    assert data["kind"] == struct.kind
    assert CONVERTER.structure(data, union.__value__) == struct


def test_union_columns_cover_all_members():
    assert table_for("symbols").columns() == ("name", "kind", "value")
    assert set(table_for("norm_steps").columns()) == {
        "name",
        "description",
        "kind",
        "input_str",
        "output_str",
        "left_context",
        "right_context",
    }


@pytest.mark.parametrize(
    "name, table_name",
    [
        ("symbols", "symbols"),
        ("Symbol", "symbols"),
        ("Token", "symbols"),
        ("NormStep", "norm_steps"),
        ("MappingNorm", "norm_steps"),
        ("GroupMember", "group_members"),
        ("Text", "texts"),
    ],
)
def test_table_for(name, table_name):
    assert table_for(name).name == table_name


def test_get_structs_filters_union_members():
    registry = init_registry()
    _, registry = build_struct("Token", registry, name="a")
    _, registry = build_struct("TokenGroup", registry, name="V")
    assert get_structs("Token", registry) == (Token(name="a"),)
    assert get_structs("TokenGroup", registry) == (TokenGroup(name="V"),)
    assert len(get_structs("symbols", registry)) == 2
    assert len(get_structs("Symbol", registry)) == 2


def _with_rows(table_name: str, rows: list) -> dict:
    """An unchecked registry: the view helpers don't need valid references."""
    registry = init_registry()
    return registry | {table_name: registry[table_name] + tuple(rows)}


def test_nested_views_follow_position_not_insertion_order():
    group_rows = [
        GroupMember(group="V", position=2, token="c"),
        GroupMember(group="V", position=0, token="a"),
        GroupMember(group="W", position=0, token="z"),
        GroupMember(group="V", position=1, token="b"),
    ]
    step_rows = [
        SpecStep(spec="s", position=5, step="lowercase"),
        SpecStep(spec="s", position=1, step="NFC_normalization"),
    ]
    registry = _with_rows("group_members", group_rows)
    registry = registry | {"spec_steps": tuple(step_rows)}
    assert group_members(registry, "V") == ("a", "b", "c")
    assert group_members(registry, "W") == ("z",)
    assert group_members(registry, "missing") == ()
    assert spec_steps(registry, "s") == ("NFC_normalization", "lowercase")
    assert next_position(registry, "group_members", "group", "V") == 3
    assert next_position(registry, "spec_steps", "spec", "s") == 6
    assert next_position(registry, "spec_steps", "spec", "new") == 0


def test_mapping_view():
    registry = _with_rows(
        "mapping_rows",
        [
            MappingRow(norm="m", intab="a", outtab="b"),
            MappingRow(norm="m", intab="ch", outtab="c"),
            MappingRow(norm="other", intab="x", outtab=""),
        ],
    )
    assert mapping(registry, "m") == {"a": "b", "ch": "c"}
    assert mapping(registry, "other") == {"x": ""}
