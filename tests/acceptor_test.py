"""
Regression tests for symbol table construction and pattern tokenization.
"""

import pytest

from kayino.acceptor import fsa, symbol_table_for_tokens, tokenize_str, tokens_of
from kayino.models import (
    Pattern,
    RegistryCheckError,
    Token,
    TokenGroup,
)
from kayino.structs import build_struct, init_registry


def _registry_with_tokens(*strings: str):
    registry = init_registry()
    for string in strings:
        _, registry = build_struct(Token.__name__, registry, name=string)
    return registry


def _nonempty(tokens):
    """Drop the empty AnonTokens that `re.split` leaves between adjacent matches."""
    return [tok for tok in tokens if getattr(tok, "string", None) != ""]


def _accepted_symbol_paths(fst, registry) -> set[tuple[str, ...]]:
    """All label paths through an acyclic acceptor, decoded to token strings."""
    sym_table = symbol_table_for_tokens(tokens_of(registry))
    paths = set()
    path_iter = fst.paths()
    while not path_iter.done():
        paths.add(tuple(sym_table.find(label) for label in path_iter.ilabels()))
        path_iter.next()
    return paths


def test_symbol_table_keys_skip_epsilon():
    registry = _registry_with_tokens("a", "b", "c")
    sym_table = symbol_table_for_tokens(tokens_of(registry))
    keys = [key for key, _ in sym_table]
    assert 0 not in keys
    assert len(set(keys)) == 3


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("a", {("a",)}),
        ("b", {("b",)}),
        ("ab", {("a", "b")}),
        ("ba", {("b", "a")}),
    ],
)
def test_first_token_is_not_epsilon(pattern, expected):
    registry = _registry_with_tokens("a", "b")
    assert _accepted_symbol_paths(fsa(pattern, registry), registry) == expected


def test_tokenize_recognizes_group_name():
    registry = _registry_with_tokens("a", "b")
    _, registry = build_struct(TokenGroup.__name__, registry, name="V")
    tokens = _nonempty(tokenize_str("Va", registry))
    assert [type(tok) for tok in tokens] == [TokenGroup, Token]
    assert tokens[0].name == "V"
    assert tokens[1].name == "a"


def test_tokenize_recognizes_pattern_name():
    registry = _registry_with_tokens("a", "b")
    _, registry = build_struct(Pattern.__name__, registry, name="P", value="ab")
    tokens = _nonempty(tokenize_str("Pa", registry))
    assert [type(tok) for tok in tokens] == [Pattern, Token]
    assert tokens[0].name == "P"


def test_names_unique_across_tokens_groups_and_patterns():
    registry = _registry_with_tokens("a")
    _, registry = build_struct(TokenGroup.__name__, registry, name="V")
    with pytest.raises(RegistryCheckError):
        build_struct(Token.__name__, registry, name="V")
    with pytest.raises(RegistryCheckError):
        build_struct(Pattern.__name__, registry, name="V", value="a")
