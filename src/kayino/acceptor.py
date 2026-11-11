"""
Functional FSA compilation for parC grammar.

Compiles inventory + features into a pynini SymbolTable, builds a token map
for pattern string tokenization, and compiles all patterns into pynini FSAs
via recursive descent parsing.
"""

from __future__ import annotations

import enum
import re
from functools import lru_cache
from typing import Literal, Sequence, Type

import pynini
from attrs import evolve
from pynini.lib import rewrite

from kayino.constants import MAX_OUTPUT_STRINGS
from kayino.models import (
    AnonToken,
    Pattern,
    RegexSymbols,
    RegistryType,
    Token,
    TokenGroup,
)

"""
## Module-level constants
"""


class PatternCompileStatus(enum.Enum):
    COMPILED = 0
    TOKENS_IDENTIFIED = 1
    UNRECOGNIZED_TOKENS = 2
    COMPILATION_ERROR = 3


"""
## Symbol table
"""


@lru_cache(maxsize=1)
def symbol_table_for_tokens(
    token_array: Sequence[Token] | None = None,
) -> pynini.SymbolTable:
    sym_table = pynini.SymbolTable()
    if token_array:
        for token in token_array:
            sym_table.add_symbol(token.string, key=token.id)
    return sym_table


@lru_cache(maxsize=1)
def get_sigma(token_array: Sequence[Token]) -> pynini.Fst:
    sym_table = symbol_table_for_tokens(token_array)
    sigma = pynini.union(
        pynini.accep(token.string, token_type=sym_table) for token in token_array
    )
    return sigma


@lru_cache(maxsize=1)
def get_sigma_star(token_array: Sequence[Token]) -> pynini.Fst:
    sigma = get_sigma(token_array)
    return pynini.closure(sigma)


"""
## Tokenization
"""


@lru_cache(maxsize=1)
def _token_regex(registry: RegistryType) -> re.Pattern:
    token_symbols = [token.string for token in registry.tokens]
    group_symbols = [group.string for group in registry.token_groups]
    regex_symbols = [re.escape(item.value) for item in RegexSymbols]
    return re.compile(
        "("
        + "|".join(
            sorted(
                token_symbols + group_symbols + regex_symbols,
                key=len,
                reverse=True,
            )
        )
        + ")",
    )


@lru_cache(maxsize=1)
def get_token_string_map(token_array: Sequence[Token]) -> dict[str, Token]:
    return {token.string: token for token in token_array}


@lru_cache(maxsize=1)
def get_token_id_map(token_array: Sequence[Token]) -> dict[int, Token]:
    return {token.id: token for token in token_array}


@lru_cache(maxsize=1)
def get_token_group_map(token_groups: Sequence[TokenGroup]) -> dict[int, Token]:
    return {group.string: group for group in token_groups}


def tokenize_str(
    pattern_str: str,
    registry: RegistryType,
) -> tuple[Token | AnonToken]:
    token_regex = _token_regex(registry)
    token_map = get_token_string_map(registry.tokens)
    group_map = get_token_group_map(registry.token_groups)
    tokenized = []
    split_by_tokens = token_regex.split(pattern_str)
    for word in split_by_tokens:
        if word in token_map:
            tokenized.append(token_map[word])
        elif word in group_map:
            tokenized.append(group_map[word])
        elif word in RegexSymbols:
            tokenized.append(AnonToken(string=word, kind="operator"))
        else:
            tokenized.append(AnonToken(string=word, kind="unrecognized"))

    return tuple(tokenized)


"""
## Recursive descent parser
"""


def _interpret_unary_operator(fst: pynini.Fst, op: str) -> pynini.Fst:
    if op == "?":
        return fst.ques
    if op == "+":
        return fst.plus
    if op == "*":
        return fst.star
    raise ValueError(f"Unknown unary operator: {op!r}")


def _atom_to_fst(
    token: Token | TokenGroup | AnonToken,
    registry: RegistryType,
) -> pynini.Fst:
    sym_table = symbol_table_for_tokens(registry.tokens)
    return pynini.accep(token.string, token_type=sym_table)


@lru_cache()
def _group_or_pattern_to_fst(token: TokenGroup) -> pynini.Fst:
    pass


def _parse_factor_sequence(
    tokens: list[Token],
    i: int,
    registry: RegistryType,
) -> tuple[list[pynini.Fst], int]:
    fsas: list[pynini.Fst] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind in ("right_delimiter", "pipe_operator", "unary_operator"):
            break
        if tok.kind == "left_delimiter":
            f, i = _parse_delimited_factor(
                tokens,
                i,
                registry,
            )
            fsas.append(f)
        else:
            fsas.append(_atom_to_fst(tok, registry))
            i += 1
    return fsas, i


def _parse_delimited_factor(
    tokens: list[Token],
    i: int,
    registry: RegistryType,
) -> tuple[pynini.Fst, int]:
    left = tokens[i].string
    i += 1
    if left == "{":
        negated = i < len(tokens) and tokens[i].kind == "caret_operator"
        if negated:
            i += 1
        factors, i = _parse_factor_sequence(
            tokens,
            i,
            registry,
        )
        inner = pynini.union(*factors)
        if negated:
            inner = pynini.difference(get_sigma(registry.tokens), inner)
        expected = "}"
    elif left == "(":
        inner, i = _parse_expression(
            tokens,
            i,
            registry,
        )
        expected = ")"
    else:
        raise ValueError(f"Unexpected left delimiter: {left!r}")
    if tokens[i].string != expected:
        raise ValueError(f"Expected '{expected}' but got '{tokens[i].string}'")
    return inner, i + 1


def _parse_term(
    tokens: list[Token],
    i: int,
    registry: RegistryType,
) -> tuple[pynini.Fst, int]:
    if tokens[i].kind in ("right_delimiter", "pipe_operator"):
        raise ValueError(f"Unexpected {tokens[i].kind!r} token at start of term")
    fsas: list[pynini.Fst] = []
    while i < len(tokens) and tokens[i].kind not in (
        "right_delimiter",
        "pipe_operator",
    ):
        factor_list, i = _parse_factor_sequence(
            tokens,
            i,
            registry,
        )
        if i < len(tokens) and tokens[i].kind == "unary_operator":
            if not factor_list:
                raise ValueError("Unary operator with no preceding factor")
            factor_list[-1] = _interpret_unary_operator(
                factor_list[-1], tokens[i].string
            )
            i += 1
        fsas.extend(factor_list)
    if not fsas:
        raise ValueError("Empty term")
    result = fsas[0]
    for f in fsas[1:]:
        result = pynini.concat(result, f)
    return result, i


def _parse_expression(
    tokens: list[Token],
    i: int,
    registry: RegistryType,
) -> tuple[pynini.Fst, int]:
    term, i = _parse_term(tokens, i, registry)
    terms = [term]
    while i < len(tokens) and tokens[i].kind == "pipe_operator":
        i += 1
        t, i = _parse_term(tokens, i, registry)
        terms.append(t)
        if i >= len(tokens) or tokens[i].kind == "right_delimiter":
            break
    return pynini.union(*terms), i


def _parse_tokens(
    tokens: list[Token],
    registry: RegistryType,
) -> pynini.Fst:
    fst, end = _parse_expression(
        tokens,
        0,
        registry,
    )
    if end != len(tokens):
        raise ValueError(f"Leftover tokens after parse: {tokens[end:]}")
    return fst


def _parse_pattern(
    pattern_str: str,
    registry: RegistryType,
) -> pynini.Fst:
    sym_table = symbol_table_for_tokens(registry.tokens)
    if not pattern_str:
        return pynini.accep("", token_type=sym_table)
    toks = tokenize_str(pattern_str, registry)
    fst = _parse_tokens(toks, registry)
    fst.optimize()
    return fst


"""
### String -> FSA
"""


def fsa(pattern_str: str, registry: RegistryType) -> pynini.Fst:
    return _parse_pattern(
        pattern_str,
        registry,
    )


"""
### FSA -> string
"""


def decode_labels(
    label_iter,
    registry: RegistryType,
) -> str:
    sym_table = symbol_table_for_tokens(registry.tokens)
    word = ""
    for label in label_iter:
        if label == 0:
            continue
        symbol = sym_table.find(label)
        word += symbol
    return word


def fsm_strings_and_weights(
    fst: pynini.Fst,
    registry: RegistryType,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
    strip_word_edge_symbols: bool = False,
    strip_all_tags: bool = False,
) -> list[tuple[str, float]]:
    projected = pynini.project(fst, project_type=project)
    if nshortest is not None:
        projected = rewrite.lattice_to_nshortest(projected, nshortest=nshortest)
    seen: set[str] = set()
    decoded: list[tuple[str, float]] = []
    path_iter = projected.paths()
    while not path_iter.done():
        word = decode_labels(
            path_iter.olabels(),
            registry,
            strip_word_edge_symbols=strip_word_edge_symbols,
            strip_all_tags=strip_all_tags,
        )
        if word not in seen:
            seen.add(word)
            decoded.append((word, float(path_iter.weight())))
        path_iter.next()
    decoded.sort(key=lambda t: t[1])
    return decoded


def fsm_strings(
    fst: pynini.Fst,
    registry: RegistryType,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
    strip_word_edge_symbols: bool = False,
    strip_all_tags: bool = False,
) -> list[str]:
    return [
        s
        for s, _ in fsm_strings_and_weights(
            fst,
            registry,
            project,
            nshortest,
            strip_word_edge_symbols,
            strip_all_tags,
        )
    ]


def fsm_string(
    fst: pynini.Fst,
    registry: RegistryType,
    project: Literal["input", "output"] = "output",
    strip_word_edge_symbols: bool = False,
    strip_all_tags: bool = False,
) -> str:
    strings = fsm_strings(
        fst=fst,
        registry=registry,
        project=project,
        nshortest=1,
        strip_word_edge_symbols=strip_word_edge_symbols,
        strip_all_tags=strip_all_tags,
    )
    if len(strings) != 1:
        raise ValueError(f"Expected single string, got {strings}")
    return strings[0]


def filter_strings_by_pattern(
    input_fst: pynini.Fst,
    pattern_fst: pynini.Fst,
    registry: RegistryType,
) -> list[str]:
    return fsm_strings(
        pynini.intersect(input_fst, pattern_fst).optimize(),
        registry,
        nshortest=None,
    )
