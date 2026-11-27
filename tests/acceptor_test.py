import json
from graphlib import CycleError
from pathlib import Path

import pynini
import pytest

from kayino.acceptor import (
    _parse_pattern,
    decode_labels,
    fsa,
    fsm_string,
    fsm_strings,
    fsm_strings_and_weights,
    symbol_table_for_tokens,
)
from kayino.models import Pattern, Registry, Token
from kayino.prebuilt import _normalize_whitespace

CASE_DIR = Path(__file__).parent / "test_cases"
CASES = {
    category: [
        json.loads(line)
        for line in (CASE_DIR / f"acceptor_{category}_tests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for category in ("compilation", "decoding", "regex")
}


def _parametrize(category, group, *fields):
    params = [
        pytest.param(*(case[field] for field in fields), id=case.get("id"))
        for case in CASES[category]
        if case["group"] == group
    ]
    assert params, f"No {category} cases for {group}"
    return pytest.mark.parametrize(fields, params)


@pytest.fixture
def case(request):
    group = request.function.__name__.removeprefix("test_")
    (case,) = [
        row for cases in CASES.values() for row in cases if row["group"] == group
    ]
    return case


def test_fsa_with_symbol_table_compiles_registered_token(case):
    token = Token(case["token"])
    compiled = fsa(case["expression"], Registry(Symbol=(token,)), use_symbol_table=True)

    assert (
        compiled.string(token_type=symbol_table_for_tokens((token,))) == case["token"]
    )
    assert compiled.num_arcs(compiled.start()) == case["arcs"]


def test_fsa_with_symbol_table_rejects_oov_symbol(case):
    registry = Registry(Symbol=(Token(case["token"]),))

    with pytest.raises(pynini.FstStringCompilationError):
        fsa(case["expression"], registry, use_symbol_table=True)


@_parametrize("compilation", "parse_pattern_uses_provided_symbol_table", "with_table")
def test_parse_pattern_uses_provided_symbol_table(with_table):
    token = Token("ch")
    table = symbol_table_for_tokens((token,)) if with_table else None
    compiled = _parse_pattern("ch", Registry(Symbol=(token,)), sym_table=table)

    assert compiled.string(token_type=table if with_table else "byte") == "ch"
    assert compiled.num_states() == (2 if with_table else 3)


@_parametrize(
    "compilation",
    "fsa_without_symbol_table_accepts_oov_symbols",
    "inventory",
)
def test_fsa_without_symbol_table_accepts_oov_symbols(inventory):
    registry = Registry(Symbol=tuple(Token(token) for token in inventory))
    compiled = fsa("ch ŋ", registry, use_symbol_table=False)

    assert compiled.string(token_type="byte") == "ch ŋ"


@_parametrize("compilation", "fsa_expands_registered_patterns", "use_symbol_table")
def test_fsa_expands_registered_patterns(use_symbol_table):
    token = Token("ch")
    registry = Registry(
        Symbol=(token, Pattern("<Inner>", "ch"), Pattern("<Outer>", "<Inner>"))
    )
    token_type = symbol_table_for_tokens((token,)) if use_symbol_table else "byte"

    compiled = fsa("<Outer>", registry, use_symbol_table=use_symbol_table)

    assert compiled.string(token_type=token_type) == "ch"


@_parametrize("compilation", "fsa_rejects_recursive_patterns", "patterns")
def test_fsa_rejects_recursive_patterns(patterns):
    registry = Registry(
        Symbol=(Token("ch"), *(Pattern(*pattern) for pattern in patterns))
    )

    with pytest.raises(CycleError):
        fsa("<First>", registry)


@pytest.fixture(
    params=[row for row in CASES["decoding"] if row["group"] == "decoding_case"],
    ids=lambda row: row["id"],
)
def decoding_case(request):
    row = request.param
    inventory = tuple(Token(token) for token in row["inventory"])
    registry = Registry(Symbol=inventory)
    source = row["source"]
    expected = row["expected"]
    if row["use_symbol_table"]:
        table = symbol_table_for_tokens(inventory)
        return (
            registry,
            table,
            expected,
            [table.find(token) for token in source.split()],
            source,
        )

    return registry, None, expected, list(source.encode()), source


def test_decode_labels(decoding_case):
    _, table, expected, labels, _ = decoding_case

    assert decode_labels(iter([0, *labels, 0]), sym_table=table) == expected


@pytest.mark.parametrize(
    ("decoder", "expected_format"),
    [
        (fsm_string, lambda text: text),
        (fsm_strings, lambda text: [text]),
        (fsm_strings_and_weights, lambda text: [(text, 2.5)]),
    ],
    ids=["string", "strings", "weighted-strings"],
)
def test_fst_decoders(decoding_case, decoder, expected_format):
    registry, table, expected, _, source = decoding_case
    token_type = table if table is not None else "byte"
    fst = pynini.accep(source, weight=2.5, token_type=token_type)

    assert decoder(
        fst, registry, use_symbol_table=table is not None
    ) == expected_format(expected)


@_parametrize(
    "compilation",
    "empty_expression_accepts_only_epsilon",
    "use_symbol_table",
)
def test_empty_expression_accepts_only_epsilon(use_symbol_table):
    token = Token("ch")
    registry = Registry(Symbol=(token,))
    token_type = symbol_table_for_tokens((token,)) if use_symbol_table else "byte"
    compiled = fsa("", registry, use_symbol_table=use_symbol_table)

    assert compiled.string(token_type=token_type) == ""


def test_pattern_cache_keeps_registries_separate(case):
    for value in case["values"]:
        registry = Registry(Symbol=(Pattern(case["pattern"], value),))

        assert fsa(case["pattern"], registry).string(token_type="byte") == value


@_parametrize(
    "compilation",
    "grouped_alternation_with_optional_suffix",
    "use_symbol_table",
)
def test_grouped_alternation_with_optional_suffix(use_symbol_table):
    inventory = (Token("ch"), Token("a"), Token("b"))
    registry = Registry(Symbol=inventory)
    token_type = symbol_table_for_tokens(inventory) if use_symbol_table else "byte"
    compiled = fsa("ch(a|b)?", registry, use_symbol_table=use_symbol_table)

    assert set(compiled.paths(output_token_type=token_type).ostrings()) == {
        "ch",
        "ch a" if use_symbol_table else "cha",
        "ch b" if use_symbol_table else "chb",
    }


@_parametrize(
    "decoding",
    "weighted_decoder_projects_and_limits_paths",
    "project",
    "expected",
)
def test_weighted_decoder_projects_and_limits_paths(project, expected):
    expected = [tuple(path) for path in expected]
    fst = pynini.union(
        pynini.cross("ŋ", pynini.accep("é", weight=1.0, token_type="byte")),
        pynini.cross("𠮷", pynini.accep("", weight=3.0, token_type="byte")),
    )

    assert (
        fsm_strings_and_weights(fst, Registry(), project=project, nshortest=1)
        == (expected[:1])
    )
    assert dict(
        fsm_strings_and_weights(fst, Registry(), project=project, nshortest=None)
    ) == dict(expected)


@pytest.fixture(params=[True, False], ids=["table", "no-table"])
def regex_mode(request):
    inventory = (Token("a"), Token("b"), Token("c"))
    table = symbol_table_for_tokens(inventory) if request.param else None
    return Registry(Symbol=inventory), table


@_parametrize("regex", "regex_language", "expression", "accepted", "rejected")
def test_regex_language(regex_mode, expression, accepted, rejected):
    registry, table = regex_mode
    compiled = fsa(expression, registry, use_symbol_table=table is not None)
    token_type = table if table is not None else "byte"

    for candidate in (*accepted, *rejected):
        source = " ".join(candidate) if table is not None else candidate
        probe = pynini.accep(source, token_type=token_type)
        matches = pynini.compose(probe, compiled).start() != pynini.NO_STATE_ID

        assert matches == (candidate in accepted), repr(candidate)


@_parametrize("regex", "malformed_regex_raises_value_error", "expression")
def test_malformed_regex_raises_value_error(regex_mode, expression):
    registry, table = regex_mode

    with pytest.raises(ValueError):
        fsa(expression, registry, use_symbol_table=table is not None)


def test_quantifier_repeats_longest_registered_token(case):
    inventory = tuple(Token(token) for token in case["inventory"])
    table = symbol_table_for_tokens(inventory)
    compiled = fsa(
        case["expression"], Registry(Symbol=inventory), use_symbol_table=True
    )

    assert pynini.compose(
        pynini.accep(case["accepted"], token_type=table), compiled
    ).start() != (pynini.NO_STATE_ID)
    assert (
        pynini.compose(
            pynini.accep(case["rejected"], token_type=table), compiled
        ).start()
        == pynini.NO_STATE_ID
    )


@_parametrize("regex", "byte_regex_quantifier_repeats_unicode_atom", "inventory")
def test_byte_regex_quantifier_repeats_unicode_atom(inventory):
    registry = Registry(Symbol=tuple(Token(token) for token in inventory))
    compiled = fsa("aŋ+", registry, use_symbol_table=False)

    for candidate, expected in [
        ("aŋ", True),
        ("aŋŋ", True),
        ("a", False),
        ("aŋaŋ", False),
    ]:
        probe = pynini.accep(candidate, token_type="byte")
        assert (
            pynini.compose(probe, compiled).start() != pynini.NO_STATE_ID
        ) == expected


@_parametrize("regex", "unregistered_literals_round_trip_in_byte_mode", "literal")
def test_unregistered_literals_round_trip_in_byte_mode(literal):
    registry = Registry(Symbol=(Token("ch"),))
    compiled = fsa(literal, registry, use_symbol_table=False)

    assert fsm_string(compiled, registry, use_symbol_table=False) == literal


@_parametrize(
    "regex",
    "normalize_whitespace_trims_edges_and_collapses_internal_runs",
    "source",
    "normalized",
)
def test_normalize_whitespace_trims_edges_and_collapses_internal_runs(
    source, normalized
):
    assert _normalize_whitespace(source) == normalized


@_parametrize(
    "regex",
    "fsa_normalizes_whitespace_before_parsing",
    "source",
    "normalized",
)
def test_fsa_normalizes_whitespace_before_parsing(source, normalized):
    registry = Registry(Symbol=tuple(Token(token) for token in ("ch", "a", "b", "c")))
    compiled = fsa(source, registry, use_symbol_table=False)
    expected = fsa(normalized, registry, use_symbol_table=False)

    assert pynini.equivalent(compiled, expected)


@_parametrize(
    "regex",
    "symbol_table_rejects_unregistered_character_next_to_known_token",
    "literal",
)
def test_symbol_table_rejects_unregistered_character_next_to_known_token(literal):
    registry = Registry(Symbol=(Token("ch"),))

    with pytest.raises(pynini.FstStringCompilationError):
        fsa(literal, registry, use_symbol_table=True)


@_parametrize(
    "regex",
    "byte_regex_matches_unregistered_characters",
    "expression",
    "accepted",
    "rejected",
)
def test_byte_regex_matches_unregistered_characters(expression, accepted, rejected):
    compiled = fsa(expression, Registry(Symbol=(Token("ch"),)), use_symbol_table=False)

    for candidate in (*accepted, *rejected):
        probe = pynini.accep(candidate, token_type="byte")
        matches = pynini.compose(probe, compiled).start() != pynini.NO_STATE_ID
        assert matches == (candidate in accepted), repr(candidate)


def test_pattern_expands_unregistered_characters_with_symbol_table_off(case):
    registry = Registry(Symbol=(Token(case["token"]), Pattern(*case["pattern"])))
    compiled = fsa(case["pattern"][0], registry, use_symbol_table=False)

    assert set(fsm_strings(compiled, registry, use_symbol_table=False)) == set(
        case["expected"]
    )


@_parametrize("regex", "malformed_byte_regex_with_unicode_and_whitespace", "expression")
def test_malformed_byte_regex_with_unicode_and_whitespace(expression):
    with pytest.raises(ValueError):
        fsa(expression, Registry())


def test_whitespace_only_expression_matches_empty_expression(regex_mode, case):
    registry, table = regex_mode
    use_symbol_table = table is not None
    compiled = fsa(case["expression"], registry, use_symbol_table=use_symbol_table)
    empty = fsa("", registry, use_symbol_table=use_symbol_table)

    assert pynini.equivalent(compiled, empty)


def test_fsa_compiles_without_registry(case):
    compiled = fsa(case["expression"])

    assert set(compiled.paths(output_token_type="byte").ostrings()) == set(
        case["expected"]
    )


def test_fsa_compiles_empty_expression_without_registry(case):
    assert fsa(case["expression"]).string(token_type="byte") == case["expected"]


@_parametrize(
    "compilation",
    "fsa_rejects_pattern_references_without_registry",
    "expression",
)
def test_fsa_rejects_pattern_references_without_registry(expression):
    with pytest.raises(ValueError):
        fsa(expression)


def test_decode_labels_without_registry(case):
    labels = [0, *case["source"].encode("utf-8"), 0]

    assert decode_labels(iter(labels)) == case["expected"]


@_parametrize("decoding", "fst_decoders_without_registry", "decoder", "expected")
def test_fst_decoders_without_registry(decoder, expected):
    fst = pynini.accep("ŋ𠮷", weight=2.5, token_type="byte")
    decoders = {
        "fsm_string": fsm_string,
        "fsm_strings": fsm_strings,
        "fsm_strings_and_weights": fsm_strings_and_weights,
    }
    if decoder == "fsm_strings_and_weights":
        expected = [tuple(path) for path in expected]

    assert decoders[decoder](fst) == expected
