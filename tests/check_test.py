"""
Tests for registry checking, restrict/cascade deletes, replace and rename.
"""

import json
from pathlib import Path

import attrs
import pytest

from kayino.models import (
    TABLE_BY_NAME,
    KeyChangeError,
    MappingNorm,
    RegistryCheckError,
    RenameError,
    Token,
    row_type_for,
)
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState
from kayino.schema import Violation
from kayino.structs import (
    RENAMEABLE_TABLES,
    build_struct,
    check,
    fetch_struct,
    get_structs,
    group_members,
    init_registry,
    mapping,
    rename_struct,
    replace_struct,
    spec_patterns,
    spec_steps,
)

VIOLATIONS_FILE = Path(__file__).parent / "test_cases" / "registry_violations.jsonl"
VIOLATIONS = [json.loads(line) for line in VIOLATIONS_FILE.read_bytes().splitlines()]


def _row(table_name: str, data: dict) -> object:
    """Build a row directly with attrs (no cattrs validation)."""
    data = dict(data)
    table = TABLE_BY_NAME[table_name]
    if class_name := data.pop("class", None):
        cls = row_type_for(class_name)
    elif table.is_union:
        (cls,) = [c for c in table.row_classes if attrs.fields(c).kind.default == data["kind"]]
    else:
        cls = table.row_type
    return cls(**data)


def registry_from_case(case: dict):
    """An unchecked registry: prebuilt norms plus the case's rows."""
    registry = init_registry()
    for table_name, rows in case["rows"].items():
        registry = registry | {
            table_name: registry[table_name] + tuple(_row(table_name, r) for r in rows)
        }
    return registry


@pytest.mark.parametrize("case", VIOLATIONS, ids=[case["id"] for case in VIOLATIONS])
def test_check_reports_violations(case):
    registry = registry_from_case(case)
    found = {(v.table, v.kind, v.column) for v in check(registry)}
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
## REPL: restrict, cascade, and failed commands leaving state unchanged
"""


@pytest.fixture(scope="module")
def grammar():
    return create_grammar()


def run(commands: list[str], grammar, state: ReplState | None = None):
    state = state or ReplState()
    messages = []
    for command in commands:
        message, state = repl_turn(command, state, grammar)
        messages.append(message)
    return messages, state


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
def test_refused_command_leaves_state_unchanged(setup, command, expected_message, grammar):
    _, state = run(setup, grammar)
    (message,), new_state = run([command], grammar, state)
    assert new_state is state
    assert expected_message in str(message)


def test_restrict_does_not_block_cascade(grammar):
    # Removing a group takes its members with it even though they reference tokens.
    _, state = run(["add tok a", "add tkg <V> a", "remove tkg <V>", "remove tok a"], grammar)
    assert state.registry["symbols"] == ()
    assert state.registry["group_members"] == ()


"""
## replace
"""


def test_replace_keeps_position():
    _, registry = build_struct("Token", init_registry(), name="a")
    norm, registry = build_struct("MappingNorm", registry, name="m")
    _, registry = build_struct("MappingNorm", registry, name="n")
    registry = replace_struct(norm, attrs.evolve(norm, description="d"), registry)
    names = [step.name for step in get_structs(MappingNorm.__name__, registry)]
    assert names == ["m", "n"]
    assert fetch_struct("MappingNorm", "name", "m", registry).description == "d"


def test_replace_refuses_key_change():
    token, registry = build_struct("Token", init_registry(), name="a")
    with pytest.raises(KeyChangeError):
        replace_struct(token, Token(name="b"), registry)


"""
## rename
"""


def test_renameable_tables():
    assert RENAMEABLE_TABLES == {"symbols", "norm_steps", "specs"}


def _registry(*commands):
    _, state = run(list(commands), create_grammar())
    return state


def test_rename_token_updates_groups_and_pattern_strings():
    state = _registry("add tok a", "add tok b", "add tkg <V> a b", "add pat <P> ab")
    registry = rename_struct("Token", "a", "c", state.registry)
    assert group_members(registry, "<V>") == ("c", "b")
    assert fetch_struct("Pattern", "name", "<P>", registry).value == "cb"
    assert [t.name for t in get_structs("Token", registry)] == ["c", "b"]


def test_rename_norm_updates_spec_steps_and_mapping_rows():
    state = _registry("add map m a > b", "add spec s", "spec act s", "spec add step m")
    registry = rename_struct("MappingNorm", "m", "m2", state.registry)
    assert spec_steps(registry, "s") == ("m2",)
    assert mapping(registry, "m2") == {"a": "b"}
    assert mapping(registry, "m") == {}


def test_rename_spec_updates_children_and_active_spec(grammar):
    _, state = run(
        [
            "add tok a",
            "add pat <P> a",
            "add spec s",
            "spec act s",
            "spec add step lowercase",
        ],
        grammar,
    )
    _, state = run(["mv spec s t"], grammar, state)
    assert state.active_spec == "t"
    assert spec_steps(state.registry, "t") == ("lowercase",)
    assert spec_steps(state.registry, "s") == ()


def test_rename_to_existing_name_is_refused():
    state = _registry("add tok a", "add tok b")
    with pytest.raises(RegistryCheckError):
        rename_struct("Token", "a", "b", state.registry)


def test_rename_that_shifts_tokenization_is_refused():
    # "ab" tokenizes as a + b. A token named "ab" would swallow both by
    # longest match, silently changing the pattern's meaning.
    state = _registry("add tok a", "add tok b", "add tok x", "add pat <P> ab")
    with pytest.raises(RenameError, match="<P>"):
        rename_struct("Token", "x", "ab", state.registry)


def test_rename_unsupported_table_is_refused():
    with pytest.raises(RenameError):
        rename_struct("Text", "0", "1", init_registry())


def test_rename_missing_row(grammar):
    (message,), state = run(["mv tok a b"], grammar)
    assert "StructNotFoundError" in str(message)
