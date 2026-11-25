"""
Tests for table declarations, the tagged-union converter, the generated
SQLite schema, and reading rows back (select).
"""

import pytest
from attrs import frozen

from kayino.models import (
    CONVERTER,
    TABLES,
    CdrewriteNorm,
    MappingNorm,
    NormStep,
    Pattern,
    PrebuiltNorm,
    Symbol,
    Token,
    TokenGroup,
    table_for,
)
from kayino.schema import Table, check_tables, ref
from kayino.structs import change, connect, insert, select


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


def test_select_filters_union_members():
    db = connect()
    with change(db):
        insert(db, "Token", name="a")
        insert(db, "TokenGroup", name="V")
    assert select(db, "Token") == (Token(name="a"),)
    assert select(db, "TokenGroup") == (TokenGroup(name="V"),)
    assert len(select(db, "symbols")) == 2
    assert len(select(db, "Symbol")) == 2
    assert select(db, "Token", name="V") == ()


def test_select_orders_children_by_position_not_insertion():
    db = connect()
    with change(db):
        for name in ["a", "b", "c", "z"]:
            insert(db, "Token", name=name)
        insert(db, "TokenGroup", name="V")
        insert(db, "TokenGroup", name="W")
        for position, token in [(2, "c"), (0, "a"), (1, "b")]:
            insert(db, "GroupMember", group="V", position=position, token=token)
        insert(db, "GroupMember", group="W", position=0, token="z")
        insert(db, "NormalizationSpec", name="s")
        insert(db, "SpecStep", spec="s", position=5, step="lowercase")
        insert(db, "SpecStep", spec="s", position=1, step="NFC_normalization")
    assert [m.token for m in select(db, "GroupMember", group="V")] == ["a", "b", "c"]
    assert [m.token for m in select(db, "GroupMember", group="W")] == ["z"]
    assert select(db, "GroupMember", group="missing") == ()
    assert [s.step for s in select(db, "SpecStep", spec="s")] == ["NFC_normalization", "lowercase"]


def test_select_mapping_rows():
    db = connect()
    with change(db):
        insert(db, "MappingNorm", name="m")
        insert(db, "MappingNorm", name="other")
        insert(db, "MappingRow", norm="m", intab="a", outtab="b")
        insert(db, "MappingRow", norm="m", intab="ch", outtab="c")
        insert(db, "MappingRow", norm="other", intab="x", outtab="")
    assert {r.intab: r.outtab for r in select(db, "MappingRow", norm="m")} == {"a": "b", "ch": "c"}
    assert {r.intab: r.outtab for r in select(db, "MappingRow", norm="other")} == {"x": ""}
