"""
Tests for the constraints SQLite enforces and the custom checks, restrict and
cascade deletes, refused commands, and rename.
"""

import json
from pathlib import Path

import pytest

from helpers import run, snapshot, write_project
from kayino.models import RegistryCheckError, RenameError
from kayino.repl.grammar import create_grammar
from kayino.schema import Violation
from kayino.store import validate_project
from kayino.structs import RENAMEABLE_TABLES, connect, fetch, rename, select

VIOLATIONS_FILE = Path(__file__).parent / "test_cases" / "registry_violations.jsonl"
VIOLATIONS = [json.loads(line) for line in VIOLATIONS_FILE.read_bytes().splitlines()]


@pytest.mark.parametrize("case", VIOLATIONS, ids=[case["id"] for case in VIOLATIONS])
def test_violations(case, tmp_path):
    write_project(tmp_path, case["rows"])
    found = {(v.table, v.kind, v.column) for v in validate_project(tmp_path)}
    assert found == {tuple(expected) for expected in case["expected"]}


def test_violation_message():
    violation = Violation(
        table="group_members",
        key=("<V>", 1),
        column="token",
        kind="ref",
        note="'a' not found in symbols.name",
    )
    assert str(violation) == "group_members ('<V>', 1).token: 'a' not found in symbols.name"


"""
## REPL: restrict, cascade, and refused commands leaving the registry unchanged
"""


@pytest.fixture(scope="module")
def grammar():
    return create_grammar()


@pytest.mark.parametrize(
    "setup, command, expected_message",
    [
        (
            ["add tok a", "add tok b", "add tkg <V> a b"],
            "remove tok a",
            "group_members ('<V>', 0).token: 'a' not found",
        ),
        (
            ["add tok a", "add pat <P> a"],
            "remove tok a",
            "does not compile",
        ),
        (
            ["add map m a > b", "add spec s", "spec act s", "spec add step m"],
            "remove map m",
            "spec_steps ('s', 0).step: 'm' not found",
        ),
        (
            ["add tok a", "add tok b"],
            "add tkg <V> a c",
            "'c' not found in symbols.name",
        ),
        (
            [],
            "add map m a > b; a > c",
            "mapping_rows ('m', 'a'): duplicate key",
        ),
        (
            ["add tok a", "add tkg <V> a"],
            "add tok <V>",
            "",  # grammar: Token names are \w+, so this does not parse
        ),
    ],
)
def test_refused_command_leaves_registry_unchanged(setup, command, expected_message, grammar):
    _, state = run(setup, grammar)
    before = snapshot(state.db)
    (message,), new_state = run([command], grammar, state)
    assert snapshot(new_state.db) == before
    assert expected_message in message


def test_restrict_does_not_block_cascade(grammar):
    # Removing a group takes its members with it even though they reference tokens.
    _, state = run(["add tok a", "add tkg <V> a", "remove tkg <V>", "remove tok a"], grammar)
    assert select(state.db, "symbols") == ()
    assert select(state.db, "group_members") == ()


"""
## rename
"""


def test_renameable_tables():
    assert RENAMEABLE_TABLES == {"symbols", "norm_steps", "specs"}


def _db(*commands):
    return run(list(commands), create_grammar())[1].db


def test_rename_token_updates_groups_and_pattern_strings():
    db = _db("add tok a", "add tok b", "add tkg <V> a b", "add pat <P> ab")
    rename(db, "Token", "a", "c")
    assert [m.token for m in select(db, "GroupMember", group="<V>")] == ["c", "b"]
    assert fetch(db, "Pattern", name="<P>").value == "cb"
    assert [t.name for t in select(db, "Token")] == ["c", "b"]


def test_rename_norm_updates_spec_steps_and_mapping_rows():
    db = _db("add map m a > b", "add spec s", "spec act s", "spec add step m")
    rename(db, "MappingNorm", "m", "m2")
    assert [s.step for s in select(db, "SpecStep", spec="s")] == ["m2"]
    assert [(r.norm, r.intab, r.outtab) for r in select(db, "MappingRow")] == [("m2", "a", "b")]


def test_rename_spec_updates_children_and_active_spec(grammar):
    _, state = run(
        ["add tok a", "add pat <P> a", "add spec s", "spec act s", "spec add step lowercase"],
        grammar,
    )
    _, state = run(["mv spec s t"], grammar, state)
    assert state.active_spec == "t"
    assert [s.step for s in select(state.db, "SpecStep", spec="t")] == ["lowercase"]
    assert select(state.db, "SpecStep", spec="s") == ()


def test_rename_to_existing_name_is_refused():
    db = _db("add tok a", "add tok b")
    before = snapshot(db)
    with pytest.raises(RegistryCheckError):
        rename(db, "Token", "a", "b")
    assert snapshot(db) == before


def test_rename_that_shifts_tokenization_is_refused():
    # "ab" tokenizes as a + b. A token named "ab" would swallow both by
    # longest match, silently changing the pattern's meaning.
    db = _db("add tok a", "add tok b", "add tok x", "add pat <P> ab")
    before = snapshot(db)
    with pytest.raises(RenameError, match="<P>"):
        rename(db, "Token", "x", "ab")
    assert snapshot(db) == before


def test_rename_unsupported_table_is_refused():
    with pytest.raises(RenameError):
        rename(connect(), "Text", "0", "1")


def test_rename_missing_row(grammar):
    (message,), _ = run(["mv tok a b"], grammar)
    assert "StructNotFoundError" in message
