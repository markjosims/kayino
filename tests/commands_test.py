"""
REPL-level tests: commands that create or remove child rows.
"""

import pytest

from kayino.core import normalize
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState, get_active_spec
from kayino.structs import group_members, mapping, spec_steps


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


def test_add_token_group_creates_member_rows(grammar):
    _, state = run(["add tok a", "add tok b", "add tkg <V> a b"], grammar)
    assert group_members(state.registry, "<V>") == ("a", "b")


def test_add_mapping_creates_mapping_rows(grammar):
    _, state = run(["add map m a > b; ch > c"], grammar)
    assert mapping(state.registry, "m") == {"a": "b", "ch": "c"}


def test_spec_add_and_remove_steps(grammar):
    _, state = run(
        [
            "add map m a > b",
            "add spec s",
            "spec act s",
            "spec add step lowercase",
            "spec add step m",
            "spec add step lowercase",
        ],
        grammar,
    )
    assert spec_steps(state.registry, "s") == ("lowercase", "m", "lowercase")

    _, state = run(["spec rm step lowercase"], grammar, state)
    assert spec_steps(state.registry, "s") == ("m",)


def test_normalize_uses_spec_rows(grammar):
    _, state = run(
        [
            "add map m a > b",
            "add spec s",
            "spec act s",
            "spec add step lowercase",
            "spec add step m",
        ],
        grammar,
    )
    text, status, _ = normalize("AAx", get_active_spec(state), state.registry)
    assert text == "bbx"
    assert status == "licit"


def test_list_shows_nested_views(grammar):
    messages, _ = run(
        ["add tok a", "add tok b", "add tkg <V> a b", "list tkg"], grammar
    )
    rendered = "".join(text for _, text in messages[-1])
    assert "members" in rendered
    assert "a b" in rendered


def test_remove_group_cascades_to_members(grammar):
    _, state = run(
        [
            "add tok a",
            "add tok b",
            "add tkg <V> a b",
            "remove tkg <V>",
            "add tkg <V> b",
        ],
        grammar,
    )
    assert group_members(state.registry, "<V>") == ("b",)
    assert len(state.registry["group_members"]) == 1


def test_remove_mapping_cascades_to_rows(grammar):
    _, state = run(["add map m a > b; c > d", "add map n x > y", "remove map m"], grammar)
    assert mapping(state.registry, "m") == {}
    assert mapping(state.registry, "n") == {"x": "y"}


def test_remove_spec_cascades_to_steps(grammar):
    _, state = run(
        ["add spec s", "spec act s", "spec add step lowercase", "remove spec s"],
        grammar,
    )
    assert spec_steps(state.registry, "s") == ()
    assert state.registry["spec_steps"] == ()


def test_update_keeps_children():
    from attrs import evolve

    from kayino.models import MappingNorm, MappingRow
    from kayino.structs import build_struct, init_registry, replace_struct

    norm, registry = build_struct(MappingNorm.__name__, init_registry(), name="m")
    _, registry = build_struct(
        MappingRow.__name__, registry, norm="m", intab="a", outtab="b"
    )
    registry = replace_struct(norm, evolve(norm, description="new"), registry)
    assert mapping(registry, "m") == {"a": "b"}
