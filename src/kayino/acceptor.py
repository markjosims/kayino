"""
Compiles inventory + features into a pynini SymbolTable, builds a token map
for pattern string tokenization, and compiles all patterns into pynini FSAs
via recursive descent parsing.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Sequence
from functools import lru_cache, wraps
from typing import Literal

import pynini
from pynini.lib import rewrite

from kayino.constants import MAX_OUTPUT_STRINGS
from kayino.models import (
    LEFT_DELIMITERS,
    RIGHT_DELIMITERS,
    UNARY_OPERATORS,
    AnonToken,
    Pattern,
    RegexSymbols,
    Registry,
    ReservedSymbols,
    Symbol,
    Token,
    TokenLike,
    get_structs,
)

"""
## Decorators
"""


def sequence_as_tuple(f: Callable):
    @wraps(f)
    def g(seq):
        t = tuple(seq)
        return f(t)

    return g


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


def symbol_table_for_tokens(
    token_array: Sequence[Token] | None = None,
) -> pynini.SymbolTable:
    return _symbol_table_for_tokens_inner(tuple(token_array))


@sequence_as_tuple
@lru_cache(maxsize=1)
def _symbol_table_for_tokens_inner(
    token_array: tuple[Token, ...] | None = None,
) -> pynini.SymbolTable:

    sym_table = pynini.SymbolTable()
    sym_table.add_symbol(ReservedSymbols.EPSILON.value, key=0)
    if token_array:
        for token in token_array:
            sym_table.add_symbol(token.string)
    return sym_table


@sequence_as_tuple
@lru_cache(maxsize=1)
def get_sigma(token_array: Sequence[Token]) -> pynini.Fst:
    sym_table = symbol_table_for_tokens(token_array)
    sigma = pynini.union(
        pynini.accep(token.string, token_type=sym_table) for token in token_array
    )
    return sigma


@sequence_as_tuple
@lru_cache(maxsize=1)
def get_sigma_star(token_array: Sequence[Token]) -> pynini.Fst:
    sigma = get_sigma(token_array)
    return pynini.closure(sigma)


"""
## Tokenization
"""


@lru_cache(maxsize=1)
def _symbol_regex(registry: Registry) -> re.Pattern:
    user_symbols = [symbol.string for symbol in get_structs(Symbol.__name__, registry)]

    regex_symbols = [re.escape(item.value) for item in RegexSymbols]
    return re.compile(
        "("
        + "|".join(
            sorted(
                user_symbols + regex_symbols,
                key=len,
                reverse=True,
            )
        )
        + ")",
    )


@lru_cache(maxsize=1)
def _pattern_regex(registry: Registry) -> re.Pattern:
    pattern_symbols = [
        symbol.string for symbol in get_structs(Pattern.__name__, registry)
    ]
    return re.compile(
        "("
        + "|".join(
            sorted(
                pattern_symbols,
                key=len,
                reverse=True,
            )
        )
        + ")",
    )


@lru_cache(maxsize=1)
def _regex_regex(registry: Registry) -> re.Pattern:
    regex_symbols = [re.escape(item.value) for item in RegexSymbols]
    return re.compile(
        "("
        + "|".join(
            sorted(
                regex_symbols,
                key=len,
                reverse=True,
            )
        )
        + ")",
    )


@sequence_as_tuple
@lru_cache(maxsize=1)
def get_symbol_map(symbol_array: Sequence[Symbol]) -> dict[str, Symbol]:
    return {symbol.string: symbol for symbol in symbol_array}


def tokenize_str(
    text: str,
    registry: Registry,
    no_patterns_or_regex: bool = False,
) -> tuple[TokenLike, ...]:
    if no_patterns_or_regex:
        patterns = _pattern_regex(registry)
        if pattern_hits := list(filter(None, patterns.findall(text))):
            raise ValueError(
                f"Expected only plain tokens but got patterns: {pattern_hits}"
            )
        regex_operators = _regex_regex(registry)
        if regex_hits := regex_operators.findall(text):
            raise ValueError(
                f"Expected only plain tokens but got regex operatorrs: {regex_hits}"
            )

    token_regex = _symbol_regex(registry)
    symbol_map = get_symbol_map(
        get_structs(
            struct_type_name=Symbol.__name__,
            registry=registry,
        )
    )
    tokenized = []
    split_by_tokens = token_regex.split(text)
    for word in split_by_tokens:
        if not word.strip():
            continue
        if word in symbol_map:
            tokenized.append(symbol_map[word])
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
    token: Token | AnonToken,
    registry: Registry,
) -> pynini.Fst:
    sym_table = symbol_table_for_tokens(
        get_structs(
            struct_type_name=Token.__name__,
            registry=registry,
        )
    )
    return pynini.accep(token.string, token_type=sym_table)


def _parse_factor_sequence(
    tokens: list[Token],
    i: int,
    registry: Registry,
) -> tuple[list[pynini.Fst], int]:
    fsas: list[pynini.Fst] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok in RIGHT_DELIMITERS + UNARY_OPERATORS + (RegexSymbols.OPTIONAL,):
            break
        if tok in LEFT_DELIMITERS:
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
    registry: Registry,
) -> tuple[pynini.Fst, int]:
    left = tokens[i].string
    i += 1
    if left == "{":
        negated = i < len(tokens) and tokens[i] == RegexSymbols.CARET
        if negated:
            i += 1
        factors, i = _parse_factor_sequence(
            tokens,
            i,
            registry,
        )
        inner = pynini.union(*factors)
        if negated:
            inner = pynini.difference(
                get_sigma(
                    get_structs(
                        struct_type_name=Token.__name__,
                        registry=registry,
                    )
                ),
                inner,
            )
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
    registry: Registry,
) -> tuple[pynini.Fst, int]:
    if tokens[i] in RIGHT_DELIMITERS + (RegexSymbols.UNION,):
        raise ValueError(f"Unexpected {tokens[i]!r} token at start of term")
    fsas: list[pynini.Fst] = []
    while i < len(tokens) and tokens[i] not in (
        RIGHT_DELIMITERS + (RegexSymbols.UNION,),
    ):
        factor_list, i = _parse_factor_sequence(
            tokens,
            i,
            registry,
        )
        if i < len(tokens) and tokens[i] in UNARY_OPERATORS:
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
    registry: Registry,
) -> tuple[pynini.Fst, int]:
    term, i = _parse_term(tokens, i, registry)
    terms = [term]
    while i < len(tokens) and tokens[i] == RegexSymbols.UNION:
        i += 1
        t, i = _parse_term(tokens, i, registry)
        terms.append(t)
        if i >= len(tokens) or tokens[i] in RIGHT_DELIMITERS:
            break
    return pynini.union(*terms), i


def _parse_tokens(
    tokens: list[Token],
    registry: Registry,
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
    registry: Registry,
) -> pynini.Fst:
    sym_table = symbol_table_for_tokens(
        get_structs(
            struct_type_name=Token.__name__,
            registry=registry,
        )
    )
    if not pattern_str:
        return pynini.accep("", token_type=sym_table)
    toks = tokenize_str(pattern_str, registry)
    fst = _parse_tokens(toks, registry)
    fst.optimize()
    return fst


"""
### String -> FSA
"""


def fsa(text: str, registry: Registry) -> pynini.Fst:
    return _parse_pattern(
        text,
        registry,
    )


"""
### FSA -> string
"""


def decode_labels(
    label_iter,
    registry: Registry,
) -> str:
    sym_table = symbol_table_for_tokens(
        get_structs(
            struct_type_name=Token.__name__,
            registry=registry,
        )
    )
    word = ""
    for label in label_iter:
        if label == 0:
            continue
        symbol = sym_table.find(label)
        word += symbol
    return word


def fsm_strings_and_weights(
    fst: pynini.Fst,
    registry: Registry,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
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
        )
        if word not in seen:
            seen.add(word)
            decoded.append((word, float(path_iter.weight())))
        path_iter.next()
    decoded.sort(key=lambda t: t[1])
    return decoded


def fsm_strings(
    fst: pynini.Fst,
    registry: Registry,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
) -> list[str]:
    return [
        s
        for s, _ in fsm_strings_and_weights(
            fst,
            registry,
            project,
            nshortest,
        )
    ]


def fsm_string(
    fst: pynini.Fst,
    registry: Registry,
    project: Literal["input", "output"] = "output",
) -> str:
    strings = fsm_strings(
        fst=fst,
        registry=registry,
        project=project,
        nshortest=1,
    )
    if len(strings) != 1:
        raise ValueError(f"Expected single string, got {strings}")
    return strings[0]


def filter_strings_by_pattern(
    input_fsa: pynini.Fst,
    pattern_fsa: pynini.Fst,
    registry: Registry,
) -> list[str]:
    return fsm_strings(
        pynini.intersect(input_fsa, pattern_fsa).optimize(),
        registry,
        nshortest=None,
    )
