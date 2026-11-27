"""
Compiles inventory + features into a pynini SymbolTable, builds a token map
for pattern string tokenization, and compiles all patterns into pynini FSAs
via recursive descent parsing.
"""

from __future__ import annotations

import enum
import re
from array import array
from collections import defaultdict
from collections.abc import Callable, Sequence
from functools import lru_cache, wraps
from graphlib import TopologicalSorter
from typing import Literal

import pynini
from pynini.lib import rewrite
from pynini.lib.byte import BYTE

from kayino.constants import EMPTY_SYMBOL, MAX_OUTPUT_STRINGS, SIGMA_SYMBOL
from kayino.models import (
    LEFT_DELIMITERS,
    RIGHT_DELIMITERS,
    STRING_TO_OPERATOR,
    UNARY_OPERATORS,
    OovToken,
    Operator,
    Pattern,
    RegexSymbols,
    Registry,
    Symbol,
    Token,
    TokenLike,
    get_structs,
)
from kayino.prebuilt import universal_preproc

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


_pattern_re = re.compile(r"(<[^>]+>)")


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
    sym_table.add_symbol(EMPTY_SYMBOL, key=0)
    if token_array:
        for token in token_array:
            sym_table.add_symbol(token.string)
    return sym_table


@lru_cache
def get_sigma(
    use_symbol_table: bool = False,
    registry: Registry | None = None,
) -> pynini.Fst:
    if registry is None:
        return byte_star()
    else:
        token_array = get_structs(
            struct_type_name=Token.__name__,
            registry=registry,
        )
        if not token_array:
            return pynini.accep("")
        if use_symbol_table:
            sym_table = symbol_table_for_tokens(token_array)
        else:
            sym_table = None
        sigma = pynini.union(
            *(pynini.accep(token.string, token_type=sym_table) for token in token_array)
        )
    return sigma


@lru_cache(maxsize=1)
def byte_star():
    byte_star = pynini.closure(BYTE)
    return byte_star


@lru_cache
def get_sigma_star(
    use_symbol_table: bool = False,
    registry: Registry | None = None,
) -> pynini.Fst:
    sigma = get_sigma(
        use_symbol_table=use_symbol_table,
        registry=registry,
    )
    return pynini.closure(sigma)


"""
## Tokenization
"""


@lru_cache(maxsize=1)
def _symbol_regex(registry: Registry) -> re.Pattern:
    user_symbols = [symbol.string for symbol in get_structs(Symbol.__name__, registry)]

    regex_symbols = [re.escape(item.value.string) for item in RegexSymbols]
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
def _operator_regex() -> re.Pattern:
    regex_symbols = [re.escape(item.value.string) for item in RegexSymbols]
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
    text: str | None,
    registry: Registry | None = None,
    no_patterns_or_regex: bool = False,
) -> tuple[TokenLike, ...]:
    if not text:
        return tuple()

    # since <SIGMA> matches a <PatternName> shape,
    # eagerly convert it to the dot operator to prevent
    # any spurious errors
    text = text.replace(
        SIGMA_SYMBOL,
        RegexSymbols.DOT.value.string,
    )

    text = universal_preproc(text)
    regex_operators = _operator_regex()
    if registry is None:
        if pattern_hits := _pattern_re.findall(text):
            raise ValueError(
                f"Expected only plain tokens but got patterns: {pattern_hits}"
            )

    if no_patterns_or_regex:
        if pattern_hits := _pattern_re.findall(text):
            raise ValueError(
                f"Expected only plain tokens but got patterns: {pattern_hits}"
            )
        if regex_hits := regex_operators.findall(text):
            raise ValueError(
                f"Expected only plain tokens but got regex operatorrs: {regex_hits}"
            )

    if registry is not None:
        symbol_regex = _symbol_regex(registry)
        symbol_map = get_symbol_map(
            get_structs(
                struct_type_name=Symbol.__name__,
                registry=registry,
            )
        )
    else:
        symbol_regex = regex_operators
        symbol_map = {}
    tokenized = []
    split_by_tokens = symbol_regex.split(text)
    for word in split_by_tokens:
        if not word.strip():
            continue
        if word in symbol_map:
            tokenized.append(symbol_map[word])
        elif regex_operators.fullmatch(word):
            tokenized.append(STRING_TO_OPERATOR[word])
        else:
            for char in word:
                tokenized.append(OovToken(string=char))

    return tuple(tokenized)


"""
## Recursive descent parser
"""


def _interpret_unary_operator(fst: pynini.Fst, token: Operator) -> pynini.Fst:
    op = token.string
    if op == "?":
        return fst.ques
    if op == "+":
        return fst.plus
    if op == "*":
        return fst.star
    raise ValueError(f"Unknown unary operator: {op!r}")


def _atom_to_fst(
    token: TokenLike,
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> pynini.Fst:
    if token.kind == "pattern":
        if registry is None:
            raise ValueError(f"Cannot compile pattern {token} without registry")
        pattern_map = _init_patterns(
            registry=registry,
            use_symbol_table=sym_table is not None,
        )
        return pattern_map[token.string]
    return pynini.accep(token.string, token_type=sym_table)


def _parse_factor_sequence(
    tokens: list[Token],
    i: int,
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> tuple[list[pynini.Fst], int]:
    fsas: list[pynini.Fst] = []
    end_tokens = RIGHT_DELIMITERS + UNARY_OPERATORS + (RegexSymbols.UNION.value,)

    while i < len(tokens):
        tok = tokens[i]
        if tok in end_tokens:
            break
        if tok in LEFT_DELIMITERS:
            f, i = _parse_delimited_factor(
                tokens,
                i,
                registry,
                sym_table=sym_table,
            )
            fsas.append(f)
        else:
            fsas.append(
                _atom_to_fst(
                    tok,
                    registry,
                    sym_table=sym_table,
                )
            )
            i += 1
    return fsas, i


def _parse_delimited_factor(
    tokens: list[Token],
    i: int,
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> tuple[pynini.Fst, int]:
    left = tokens[i]
    i += 1
    if left == RegexSymbols.LEFT_BRACKET.value:
        negated = i < len(tokens) and tokens[i] == RegexSymbols.CARET.value
        if negated:
            i += 1
        factors, i = _parse_factor_sequence(
            tokens,
            i,
            registry,
            sym_table=sym_table,
        )
        inner = pynini.union(*factors)
        if negated:
            inner = pynini.difference(
                get_sigma(
                    registry=registry,
                ),
                inner,
            )
        expected = RegexSymbols.RIGHT_BRACKET.value
    elif left == RegexSymbols.LEFT_PAREN.value:
        inner, i = _parse_expression(
            tokens,
            i,
            registry,
            sym_table=sym_table,
        )
        expected = RegexSymbols.RIGHT_PAREN.value
    else:
        raise ValueError(f"Unexpected left delimiter: {left!r}")

    if len(tokens) <= i:
        raise ValueError(f"No matching right delimiter for {left!r}")
    if tokens[i] != expected:
        raise ValueError(f"Expected '{expected}' but got '{tokens[i]}'")

    return inner, i + 1


def _parse_term(
    tokens: list[Token],
    i: int,
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> tuple[pynini.Fst, int]:
    if len(tokens) <= i:
        raise ValueError(f"Expected term following operator {tokens[i - 1]}")
    end_tokens = RIGHT_DELIMITERS + (RegexSymbols.UNION.value,)
    if tokens[i] in end_tokens:
        raise ValueError(f"Unexpected {tokens[i]!r} token at start of term")
    fsas: list[pynini.Fst] = []

    while i < len(tokens) and tokens[i] not in end_tokens:
        factor_list, i = _parse_factor_sequence(
            tokens,
            i,
            registry,
            sym_table=sym_table,
        )
        if i < len(tokens) and tokens[i] in UNARY_OPERATORS:
            if not factor_list:
                raise ValueError("Unary operator with no preceding factor")
            factor_list[-1] = _interpret_unary_operator(factor_list[-1], tokens[i])
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
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> tuple[pynini.Fst, int]:
    term, i = _parse_term(
        tokens,
        i,
        registry,
        sym_table=sym_table,
    )
    terms = [term]
    while i < len(tokens) and tokens[i] == RegexSymbols.UNION.value:
        i += 1
        t, i = _parse_term(
            tokens,
            i,
            registry,
            sym_table=sym_table,
        )
        terms.append(t)
        if i >= len(tokens) or tokens[i] in RIGHT_DELIMITERS:
            break
    return pynini.union(*terms), i


def _parse_tokens(
    tokens: list[Token],
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> pynini.Fst:
    fst, end = _parse_expression(
        tokens,
        0,
        registry,
        sym_table=sym_table,
    )

    if end != len(tokens):
        raise ValueError(f"Leftover tokens after parse: {tokens[end:]}")
    return fst


def _parse_pattern(
    pattern_str: str,
    registry: Registry | None = None,
    sym_table: pynini.SymbolTable | None = None,
) -> pynini.Fst:
    toks = tokenize_str(pattern_str, registry)
    if not toks:
        return pynini.accep("", token_type=sym_table)

    fst = _parse_tokens(
        toks,
        registry,
        sym_table=sym_table,
    )
    fst.optimize()
    return fst


"""
### Pattern initialization
"""


@lru_cache(maxsize=1)
def _init_patterns(
    registry: Registry,
    use_symbol_table: bool = False,
) -> dict[str, pynini.Fst]:
    dependency_graph = defaultdict(set)
    pattern_array = get_structs(
        struct_type_name=Pattern.__name__,
        registry=registry,
    )
    for pattern in pattern_array:
        subpatterns = _pattern_re.findall(pattern.value)
        dependency_graph[pattern.string] = set(subpatterns)

    topo_sort = TopologicalSorter(dependency_graph)
    sorted_patterns = topo_sort.static_order()
    pattern_map: dict[str, Pattern] = {
        pattern.string: pattern for pattern in pattern_array
    }

    expanded_patterns: dict[str, str] = {}
    compiled_patterns = {}

    # successively replace patterns with flattened string values
    # and compile each to an FSA
    for pattern in sorted_patterns:
        expanded = pattern_map[pattern].value
        for subpattern in dependency_graph[pattern]:
            expanded = expanded.replace(subpattern, expanded_patterns[subpattern])
        expanded_patterns[pattern] = expanded

        pattern_fsa = fsa(
            expanded,
            registry=registry,
            use_symbol_table=use_symbol_table,
        )
        compiled_patterns[pattern] = pattern_fsa

    return compiled_patterns


"""
### String -> FSA
"""


@lru_cache(maxsize=16384)
def fsa(
    text: str,
    registry: Registry | None = None,
    use_symbol_table: bool = False,
) -> pynini.Fst:
    if use_symbol_table:
        if registry is None:
            raise ValueError("Registry needed for symbol table compilation")
        sym_table = symbol_table_for_tokens(
            get_structs(
                registry=registry,
                struct_type_name=Symbol.__name__,
            )
        )
    else:
        sym_table = None

    return _parse_pattern(
        text,
        registry,
        sym_table=sym_table,
    )


"""
### FSA -> string
"""


def decode_labels(
    label_iter,
    sym_table: pynini.SymbolTable | None = None,
) -> str:

    if sym_table is None:
        wordbuf = array("B")
    else:
        wordbuf = ""
    for label in label_iter:
        if label == 0:
            continue
        if sym_table:
            symbol = sym_table.find(label)
            wordbuf += symbol
        else:
            wordbuf.append(label)
    if sym_table:
        word = wordbuf
    else:
        word = wordbuf.tobytes().decode("utf8")
    return word


def fsm_strings_and_weights(
    fst: pynini.Fst,
    registry: Registry | None = None,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
    use_symbol_table: bool = False,
) -> list[tuple[str, float]]:
    if use_symbol_table:
        sym_table = symbol_table_for_tokens(
            get_structs(
                registry=registry,
                struct_type_name=Symbol.__name__,
            )
        )
    else:
        sym_table = None

    projected = pynini.project(fst, project_type=project)
    if nshortest is not None:
        projected = rewrite.lattice_to_nshortest(projected, nshortest=nshortest)
    seen: set[str] = set()
    decoded: list[tuple[str, float]] = []
    path_iter = projected.paths()
    while not path_iter.done():
        word = decode_labels(
            path_iter.olabels(),
            sym_table=sym_table,
        )
        if word not in seen:
            seen.add(word)
            decoded.append((word, float(path_iter.weight())))
        path_iter.next()
    decoded.sort(key=lambda t: t[1])
    return decoded


def fsm_strings(
    fst: pynini.Fst,
    registry: Registry | None = None,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
    use_symbol_table: bool = False,
) -> list[str]:
    return [
        s
        for s, _ in fsm_strings_and_weights(
            fst,
            registry,
            project,
            nshortest,
            use_symbol_table=use_symbol_table,
        )
    ]


def fsm_string(
    fst: pynini.Fst,
    registry: Registry | None = None,
    project: Literal["input", "output"] = "output",
    use_symbol_table: bool = False,
) -> str:
    strings = fsm_strings(
        fst=fst,
        registry=registry,
        project=project,
        nshortest=1,
        use_symbol_table=use_symbol_table,
    )
    if len(strings) != 1:
        raise ValueError(f"Expected single string, got {strings}")
    return strings[0]


def filter_strings_by_pattern(
    input_fsa: pynini.Fst,
    pattern_fsa: pynini.Fst,
    registry: Registry | None = None,
    use_symbol_table: bool = False,
) -> list[str]:
    return fsm_strings(
        pynini.intersect(input_fsa, pattern_fsa).optimize(),
        registry,
        nshortest=None,
        use_symbol_table=use_symbol_table,
    )
