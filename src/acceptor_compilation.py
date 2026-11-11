"""
Functional FSA compilation for parC grammar.

Compiles inventory + features into a pynini SymbolTable, builds a token map
for pattern string tokenization, and compiles all patterns into pynini FSAs
via recursive descent parsing.
"""

# there's a lot of reduplicated logic here
# e.g. sometimes the same data is added to the symbol table,
# special FSA dict and token map separately, when this could
# be done in one pass

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from graphlib import TopologicalSorter
from typing import Literal, NamedTuple

import pynini
from loguru import logger
from pynini.lib import rewrite
from src.constants import MAX_HOMOPHONE_COUNT, MAX_OUTPUT_STRINGS
from src.models import (
    HOMOPHONE_INDEX_FEATURE,
    PARADIGM_TAG,
    FstContext,
    NestedNode,
    Node,
    Pattern,
    PhonesNode,
    Project,
    ReservedSymbols,
    StructRegistryType,
    TagsNode,
    TestResult,
    TestType,
    Token,
)

"""
## Symbol table
"""


def build_symbol_table(struct_registry: StructRegistryType) -> pynini.SymbolTable:
    sym_table = pynini.SymbolTable()
    sym_table.add_symbol(ReservedSymbols.epsilon_ref)
    for node in struct_registry["Node"].values():
        if isinstance(node, NestedNode):
            continue
        for item in node.data:
            sym_table.add_symbol(item)
    for feature in struct_registry["Feature"].values():
        for value in feature.values:
            sym_table.add_symbol(f"[{feature.id}={value}]")
    for paradigm_id in struct_registry["ParadigmFile"].keys():
        sym_table.add_symbol(f"[{PARADIGM_TAG}={paradigm_id}]")
    for i in range(MAX_HOMOPHONE_COUNT):
        sym_table.add_symbol(f"[{HOMOPHONE_INDEX_FEATURE}={i}]")

    for sym in ReservedSymbols.boundary_symbols:
        sym_table.add_symbol(sym)
    for sym in ReservedSymbols.edit_tags:
        sym_table.add_symbol(sym)
    for sym in ReservedSymbols.bow_eow_tags:
        sym_table.add_symbol(sym)
    for sym in ReservedSymbols.feature_brackets:
        sym_table.add_symbol(sym)
    return sym_table


"""
## Special FSAs
Sigma, phone, flag, boundary etc. — derived from symbol table; cached in-memory.
"""


def build_special_fsas(
    sym_table: pynini.SymbolTable,
    struct_registry: StructRegistryType,
) -> dict[str, pynini.Fst]:
    phones = []
    tags = []
    for node in struct_registry["Node"].values():
        if isinstance(node, PhonesNode):
            phones.extend(node.data)
        if isinstance(node, TagsNode):
            tags.extend(node.data)

    if not phones:
        raise ValueError("Cannot build sigma FSAs without any phones in inventory.")
    phone_fsa = pynini.union(
        *[pynini.accep(p, token_type=sym_table) for p in phones]
    ).optimize()

    for feature in struct_registry["Feature"].values():
        for value in feature.values:
            tags.append(f"[{feature.id}={value}]")
    for i in range(MAX_HOMOPHONE_COUNT):
        tags.append(f"[{HOMOPHONE_INDEX_FEATURE}={i}]")
    for paradigm_id in struct_registry["ParadigmFile"].keys():
        tags.append(f"[{PARADIGM_TAG}={paradigm_id}]")

    tag_fsa = (
        pynini.union(*[pynini.accep(t, token_type=sym_table) for t in tags]).optimize()
        if tags
        else pynini.accep("", token_type=sym_table)
    )

    affix_fsa = pynini.accep(
        ReservedSymbols.affix_boundary,
        token_type=sym_table,
    )
    clitic_fsa = pynini.accep(
        ReservedSymbols.clitic_boundary,
        token_type=sym_table,
    )
    periphrasis_fsa = pynini.accep(
        ReservedSymbols.periphrasis_break,
        token_type=sym_table,
    )
    boundary_fsa = pynini.union(
        affix_fsa,
        clitic_fsa,
        periphrasis_fsa,
    )

    feature_bracket_fsa = pynini.union(
        *[
            pynini.accep(t, token_type=sym_table)
            for t in ReservedSymbols.feature_brackets
        ]
    )

    bow_fsa = pynini.accep(ReservedSymbols.bow, token_type=sym_table)
    eow_fsa = pynini.accep(ReservedSymbols.eow, token_type=sym_table)
    word_edge_fsa = pynini.union(bow_fsa, eow_fsa)

    special_symbol = pynini.union(
        tag_fsa,
        word_edge_fsa,
        boundary_fsa,
        feature_bracket_fsa,
    ).optimize()
    special_star = pynini.closure(special_symbol).optimize()

    sigma = pynini.union(
        phone_fsa,
        tag_fsa,
        boundary_fsa,
        word_edge_fsa,
        feature_bracket_fsa,
    ).optimize()
    sigma_star = pynini.closure(sigma).optimize()

    return {
        "phone": phone_fsa,
        "flag": tag_fsa,
        "sigma": sigma,
        "sigma_star": sigma_star,
        "special": special_symbol,
        "special_star": special_star,
        "bow": bow_fsa,
        "eow": eow_fsa,
        "word_edge": word_edge_fsa,
        "boundary": boundary_fsa,
        "feature_bracket": feature_bracket_fsa,
        "affix_boundary": affix_fsa,
        "clitic_boundary": clitic_fsa,
        "periphrasis_break": periphrasis_fsa,
    }


"""
## Token map
Tokens store (value, kind) only — no embedded FSAs.
"""


def build_token_map(
    sym_table: pynini.SymbolTable,
    struct_registry: StructRegistryType,
) -> dict[str, list[Token]]:
    tokens: dict[str, list[Token]] = defaultdict(list)

    tokens["dot"].append(Token(ReservedSymbols.dot, "special_ref"))

    tokens["id"].extend(
        Token(id, "special_ref")
        for id in (
            ReservedSymbols.phone_ref,
            ReservedSymbols.flag_ref,
            ReservedSymbols.sigma_ref,
            ReservedSymbols.epsilon_ref,
            ReservedSymbols.boundary_ref,
        )
    )

    for d in ReservedSymbols.left_delimiters:
        tokens["left_delimiter"].append(Token(d, "left_delimiter"))
    for d in ReservedSymbols.right_delimiters:
        tokens["right_delimiter"].append(Token(d, "right_delimiter"))
    for op in ReservedSymbols.unary_operators:
        tokens["unary_operator"].append(Token(op, "unary_operator"))
    tokens["pipe_operator"].append(
        Token(ReservedSymbols.pipe_operator, "pipe_operator")
    )
    tokens["caret_operator"].append(
        Token(ReservedSymbols.caret_operator, "caret_operator")
    )

    tokens["tag"].append(Token(ReservedSymbols.bow, "bow_eow"))
    tokens["tag"].append(Token(ReservedSymbols.eow, "bow_eow"))

    for tag in ReservedSymbols.edit_tags:
        tokens["tag"].append(Token(tag, "edit_flag"))
    for tag in ReservedSymbols.feature_brackets:
        tokens["tag"].append(Token(tag, "tag"))
    for sym in (
        ReservedSymbols.affix_boundary,
        ReservedSymbols.clitic_boundary,
        ReservedSymbols.periphrasis_break,
    ):
        tokens["boundary"].append(Token(sym, "boundary"))

    for node_id, node in struct_registry["Node"].items():
        tokens["id"].append(Token(node_id, "id"))
        if isinstance(node, PhonesNode):
            for phone in node.data:
                tokens["phone"].append(Token(phone, "phone"))
        elif isinstance(node, TagsNode):
            for tag in node.data:
                tokens["tag"].append(Token(tag, "tag"))

    for feature in struct_registry["Feature"].values():
        for val in feature.values:
            tokens["tag"].append(Token(f"[{feature.id}={val}]", "tag"))
    for i in range(MAX_HOMOPHONE_COUNT):
        tokens["tag"].append(Token(f"[{HOMOPHONE_INDEX_FEATURE}={i}]", "tag"))
    for paradigm_id in struct_registry["ParadigmFile"].keys():
        tokens["tag"].append(Token(f"[{PARADIGM_TAG}={paradigm_id}]", "tag"))
    for pattern_id in struct_registry["Pattern"].keys():
        tokens["id"].append(Token(pattern_id, "id"))

    return {
        kind: sorted(token_list, key=len, reverse=True)
        for kind, token_list in tokens.items()
    }


"""
## Inventory class FSAs
Built separately from token map; merged into compiled_patterns before parsing.
"""


class FlatNode(NamedTuple):
    phones: list[str]
    tags: list[str]


def _flatten_node(node: Node) -> FlatNode:
    if isinstance(node, PhonesNode):
        return FlatNode(phones=list(node.data), tags=[])
    if isinstance(node, TagsNode):
        return FlatNode(tags=list(node.data), phones=[])
    phones = []
    tags = []
    for child in node.data:
        flattened = _flatten_node(child)
        phones.extend(flattened.phones)
        tags.extend(flattened.tags)
    return FlatNode(phones=phones, tags=tags)


def build_class_fsts(
    sym_table: pynini.SymbolTable,
    struct_registry: StructRegistryType,
) -> dict[str, pynini.Fst]:
    result: dict[str, pynini.Fst] = {}
    for node_id, node in struct_registry["Node"].items():
        flattened = _flatten_node(node)
        child_fsas = [
            pynini.accep(item, token_type=sym_table)
            for item in flattened.phones + flattened.tags
        ]
        result[node_id] = pynini.union(*child_fsas).optimize()
    return result


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
    tok: Token,
    fst_context: FstContext,
) -> pynini.Fst:
    if tok.kind in ("phone", "tag", "bow_eow", "edit_flag", "boundary"):
        return pynini.accep(tok.value, token_type=fst_context.sym_table)
    if tok.kind == "id":
        if tok.value not in fst_context.compiled_patterns:
            raise ValueError(f"Ref '{tok.value}' not compiled yet")
        return fst_context.compiled_patterns[tok.value]
    if tok.kind in ("special_ref", "dot"):
        if tok.value == ReservedSymbols.phone_ref:
            return fst_context.special_fsas["phone"]
        if tok.value == ReservedSymbols.flag_ref:
            return fst_context.special_fsas["flag"]
        if tok.value in (ReservedSymbols.sigma_ref, ReservedSymbols.dot):
            return fst_context.special_fsas["sigma"]
        if tok.value == ReservedSymbols.boundary_ref:
            return fst_context.special_fsas["boundary"]
        if tok.value == ReservedSymbols.epsilon_ref:
            return pynini.accep("", token_type=fst_context.sym_table)
        raise ValueError(f"Unknown special id: {tok.value!r}")
    raise ValueError(f"Cannot convert token {tok!r} to FSA")


def _parse_factor_sequence(
    tokens: list[Token],
    i: int,
    fst_context: FstContext,
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
                fst_context,
            )
            fsas.append(f)
        else:
            fsas.append(_atom_to_fst(tok, fst_context))
            i += 1
    return fsas, i


def _parse_delimited_factor(
    tokens: list[Token],
    i: int,
    fst_context: FstContext,
) -> tuple[pynini.Fst, int]:
    left = tokens[i].value
    i += 1
    if left == "{":
        negated = i < len(tokens) and tokens[i].kind == "caret_operator"
        if negated:
            i += 1
        factors, i = _parse_factor_sequence(
            tokens,
            i,
            fst_context,
        )
        inner = pynini.union(*factors)
        if negated:
            inner = pynini.difference(fst_context.sigma, inner)
        expected = "}"
    elif left == "(":
        inner, i = _parse_expression(
            tokens,
            i,
            fst_context,
        )
        expected = ")"
    else:
        raise ValueError(f"Unexpected left delimiter: {left!r}")
    if tokens[i].value != expected:
        raise ValueError(f"Expected '{expected}' but got '{tokens[i].value}'")
    return inner, i + 1


def _parse_term(
    tokens: list[Token],
    i: int,
    fst_context: FstContext,
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
            fst_context,
        )
        if i < len(tokens) and tokens[i].kind == "unary_operator":
            if not factor_list:
                raise ValueError("Unary operator with no preceding factor")
            factor_list[-1] = _interpret_unary_operator(
                factor_list[-1], tokens[i].value
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
    fst_context: FstContext,
) -> tuple[pynini.Fst, int]:
    term, i = _parse_term(tokens, i, fst_context)
    terms = [term]
    while i < len(tokens) and tokens[i].kind == "pipe_operator":
        i += 1
        t, i = _parse_term(tokens, i, fst_context)
        terms.append(t)
        if i >= len(tokens) or tokens[i].kind == "right_delimiter":
            break
    return pynini.union(*terms), i


def _parse_tokens(
    tokens: list[Token],
    fst_context: FstContext,
) -> pynini.Fst:
    fst, end = _parse_expression(
        tokens,
        0,
        fst_context,
    )
    if end != len(tokens):
        raise ValueError(f"Leftover tokens after parse: {tokens[end:]}")
    return fst


def _parse_pattern(
    pattern_str: str,
    fst_context: FstContext,
) -> pynini.Fst:
    if not pattern_str:
        return pynini.accep("", token_type=fst_context.sym_table)
    toks = _tokenize_str(pattern_str, fst_context.token_map, fst_context.phone_starts)
    fst = _parse_tokens(toks, fst_context)
    fst.optimize()
    return fst


"""
## Pattern compilation
"""


def topo_sort_patterns(patterns: dict[str, Pattern]) -> list[str]:
    dep_graph: dict[str, set[str]] = {id: set() for id in patterns}
    for pattern_id, pattern_obj in patterns.items():
        for token in re.findall(r"(<[^>]+>)", pattern_obj.pattern):
            if token in patterns:
                dep_graph[pattern_id].add(token)
    pattern_order = list(TopologicalSorter(dep_graph).static_order())
    return pattern_order


def compile_all_patterns(
    struct_registry: StructRegistryType,
    token_map: dict[str, list[Token]],
    sym_table: pynini.SymbolTable,
    special_fsas: dict[str, pynini.Fst],
) -> FstContext:
    patterns = struct_registry["Pattern"]
    pattern_order = topo_sort_patterns(patterns)
    class_fsts = build_class_fsts(sym_table, struct_registry)
    phone_starts = set()
    for node in struct_registry["Node"].values():
        if isinstance(node, PhonesNode):
            for phone in node.data:
                phone_starts.add(phone[0])

    compiled_patterns: dict[str, pynini.Fst] = dict(class_fsts)
    fst_context = FstContext(
        token_map=token_map,
        phone_starts=phone_starts,
        compiled_patterns=compiled_patterns,
        sym_table=sym_table,
        sigma=special_fsas["sigma"],
        special_fsas=special_fsas,
    )
    for pattern_id in pattern_order:
        pattern_obj = patterns[pattern_id]
        try:
            compiled_patterns[pattern_id] = _parse_pattern(
                pattern_obj.pattern,
                fst_context,
            )
        except Exception as e:
            raise ValueError(f"Error compiling pattern '{pattern_id}': {e}") from e
    return fst_context


def insert_patterns(
    patterns: dict[str, Pattern],
    project: Project,
) -> Project:
    pattern_order = topo_sort_patterns(patterns)
    new_patterns: dict[str, pynini.Fst] = {**project.fst_context.compiled_patterns}
    new_token_map = deepcopy(project.fst_context.token_map)
    new_registry = {
        **project.struct_registry,
        "Pattern": {
            **project.struct_registry["Pattern"],
            **patterns,
        },
    }
    new_fst_context = project.fst_context._replace(
        compiled_patterns=new_patterns,
        token_map=new_token_map,
    )
    for pattern_id in pattern_order:
        pattern_obj = patterns[pattern_id]
        new_registry["Pattern"][pattern_id] = pattern_obj
        new_token_map["id"].append(
            Token(
                value=pattern_id,
                kind="id",
            )
        )
        new_token_map["id"].sort(key=len, reverse=True)
        try:
            new_patterns[pattern_id] = _parse_pattern(
                pattern_obj.pattern,
                new_fst_context,
            )
        except Exception as e:
            raise ValueError(f"Error compiling pattern '{pattern_id}': {e}") from e
    new_project = project._replace(
        fst_context=new_fst_context,
        struct_registry=new_registry,
    )
    return new_project


def build_fst_context_for_project(project: Project) -> Project:
    sym_table = build_symbol_table(project.struct_registry)
    token_map = build_token_map(sym_table, project.struct_registry)
    special_fsas = build_special_fsas(sym_table, project.struct_registry)
    fst_context = compile_all_patterns(
        project.struct_registry,
        token_map,
        sym_table,
        special_fsas,
    )
    new_project = project._replace(fst_context=fst_context)
    return new_project


"""
### String -> FSA
"""


def fsa(pattern_str: str, fst_context: FstContext) -> pynini.Fst:
    return _parse_pattern(
        pattern_str,
        fst_context,
    )


def word_fsa(
    word_str: str, fst_context: FstContext, prefix: str | None = None
) -> pynini.Fst:
    tagged = ReservedSymbols.bow + word_str + ReservedSymbols.eow
    if prefix:
        tagged = prefix + tagged
    return _parse_pattern(tagged, fst_context)


def wordlist_fsa(words: list[str]) -> pynini.Fst:
    return pynini.union(*[word_fsa(w) for w in words]).optimize()


"""
### FSA -> string
"""


def decode_labels(
    label_iter,
    fst_context: FstContext,
    strip_word_edge_symbols: bool = False,
    strip_all_tags: bool = False,
) -> str:
    word = ""
    for label in label_iter:
        if label == 0:
            continue
        symbol = fst_context.sym_table.find(label)
        if strip_all_tags and symbol[0] == "[":
            continue
        if strip_word_edge_symbols and symbol in ReservedSymbols.bow_eow_tags:
            continue
        word += symbol
    return word


def fsm_strings_and_weights(
    fst: pynini.Fst,
    fst_context: FstContext,
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
            fst_context,
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
    fst_context: FstContext,
    project: Literal["input", "output"] = "output",
    nshortest: int | None = MAX_OUTPUT_STRINGS,
    strip_word_edge_symbols: bool = False,
    strip_all_tags: bool = False,
) -> list[str]:
    return [
        s
        for s, _ in fsm_strings_and_weights(
            fst,
            fst_context,
            project,
            nshortest,
            strip_word_edge_symbols,
            strip_all_tags,
        )
    ]


def fsm_string(
    fst: pynini.Fst,
    fst_context: FstContext,
    project: Literal["input", "output"] = "output",
    strip_word_edge_symbols: bool = False,
    strip_all_tags: bool = False,
) -> str:
    strings = fsm_strings(
        fst=fst,
        fst_context=fst_context,
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
    fst_context: FstContext,
) -> list[str]:
    return fsm_strings(
        pynini.intersect(input_fst, pattern_fst).optimize(), fst_context, nshortest=None
    )


def run_pattern_tests(
    pattern: Pattern,
    fst_context: FstContext,
) -> list[TestResult]:
    test_results = []
    for include_str in pattern.test_includes:
        test_results.append(
            test_pattern(
                pattern=pattern,
                fst_context=fst_context,
                input_str=include_str,
                test_type="pattern_include",
            )
        )
    for exclude_str in pattern.test_excludes:
        test_results.append(
            test_pattern(
                pattern=pattern,
                fst_context=fst_context,
                input_str=exclude_str,
                test_type="pattern_exclude",
            )
        )

    return test_results


def test_pattern(
    pattern: Pattern,
    fst_context: FstContext,
    input_str: str,
    test_type: TestType,
) -> TestResult:
    pattern_fst = fst_context.compiled_patterns[pattern.id]
    intersection = filter_strings_by_pattern(
        fsa(input_str, fst_context),
        pattern_fst,
        fst_context,
    )
    passed = (
        bool(intersection) if test_type == "pattern_include" else not bool(intersection)
    )
    return TestResult(
        test_type=test_type,
        passed=passed,
        input=input_str,
        expected=input_str if test_type == "pattern_include" else None,
        result=intersection or None,
    )


if __name__ == "__main__":
    from src.yaml.yaml_server import load_project_structs

    project = load_project_structs()
    project = build_fst_context_for_project(project)

    breakpoint()
