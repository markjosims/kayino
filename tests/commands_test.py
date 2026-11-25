"""
REPL-level tests: commands that create or remove child rows.
"""

import pytest

from kayino.core import normalize
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState, get_active_spec
from kayino.structs import select


def group_members(db, group):
    return tuple(m.token for m in select(db, "GroupMember", group=group))


def mapping(db, norm):
    return {r.intab: r.outtab for r in select(db, "MappingRow", norm=norm)}


def spec_steps(db, spec):
    return tuple(s.step for s in select(db, "SpecStep", spec=spec))


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
    assert group_members(state.db, "<V>") == ("a", "b")


def test_add_mapping_creates_mapping_rows(grammar):
    _, state = run(["add map m a > b; ch > c"], grammar)
    assert mapping(state.db, "m") == {"a": "b", "ch": "c"}


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
    assert spec_steps(state.db, "s") == ("lowercase", "m", "lowercase")

    _, state = run(["spec rm step lowercase"], grammar, state)
    assert spec_steps(state.db, "s") == ("m",)


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
    text, status, _ = normalize("AAx", get_active_spec(state), state.db)
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
    assert group_members(state.db, "<V>") == ("b",)
    assert len(select(state.db, "group_members")) == 1


def test_remove_mapping_cascades_to_rows(grammar):
    _, state = run(["add map m a > b; c > d", "add map n x > y", "remove map m"], grammar)
    assert mapping(state.db, "m") == {}
    assert mapping(state.db, "n") == {"x": "y"}


def test_remove_spec_cascades_to_steps(grammar):
    _, state = run(
        ["add spec s", "spec act s", "spec add step lowercase", "remove spec s"],
        grammar,
    )
    assert spec_steps(state.db, "s") == ()
    assert select(state.db, "spec_steps") == ()
