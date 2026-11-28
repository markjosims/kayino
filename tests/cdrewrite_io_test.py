import json
from pathlib import Path

import pytest
from pynini.lib import rewrite

from kayino.core import apply_cdrewrite, apply_step, normalize
from kayino.models import (
    CdrewriteArgs,
    CdrewriteNorm,
    NormalizationSpec,
    Pattern,
    Registry,
    Token,
)


EXCLUSION_FILE = Path(__file__).parent / "test_cases" / "cdrewrite_exclusion_tests.jsonl"
EXCLUSION_CASES = [
    json.loads(line) for line in EXCLUSION_FILE.read_text(encoding="utf-8").splitlines()
]


@pytest.mark.parametrize(
    ("rule", "text", "expected"),
    [
        pytest.param(CdrewriteArgs("ab", "z"), "ab cab ab", "z cz z", id="literal"),
        pytest.param(CdrewriteArgs("ab", "z"), "ba a b", "ba a b", id="no-match"),
        pytest.param(CdrewriteArgs("ab", "z"), "", "", id="empty-input"),
        pytest.param(CdrewriteArgs("", "ə"), "", "ə", id="insert-into-empty-input"),
        pytest.param(CdrewriteArgs("ŋ", ""), "ŋŋ", "", id="delete-entire-input"),
        pytest.param(
            CdrewriteArgs("é", "ɛ"), "é é e", "é ɛ e",
            id="canonical-forms-stay-distinct",
        ),
        pytest.param(
            CdrewriteArgs("aŋ+b", "ə"), "aŋb aŋŋb aŋaŋb ab", "ə ə aŋə ab",
            id="plus-repeats-only-preceding-unicode-atom",
        ),
        pytest.param(
            CdrewriteArgs("क्\u200dष", "क्ष"), "क्\u200dष क्ष क्\u200cष", "क्ष क्ष क्\u200cष",
            id="joiner-sensitive-match",
        ),
        pytest.param(CdrewriteArgs("x", ""), "xax xx", "a ", id="deletion"),
        pytest.param(CdrewriteArgs("x", "xx"), "xa x", "xxa xx", id="no-rescan"),
        pytest.param(CdrewriteArgs("aa", "ə"), "aaa aaaa", "əa əə", id="overlapping-targets"),
        pytest.param(CdrewriteArgs("ee", "e"), "eee", "ee", id="no-rescan-after-contraction"),
        pytest.param(CdrewriteArgs("a", "ã"), "a, a.\ta\na", "ã, ã.\tã\nã", id="preserve-delimiters"),
        pytest.param(CdrewriteArgs("𠮷", "吉"), "𠮟𠮷吉", "𠮟吉吉", id="shared-utf8-prefix"),
        pytest.param(CdrewriteArgs("ŋ", "𠮷"), "éŋ é", "é𠮷 é", id="unicode-output"),
        pytest.param(CdrewriteArgs("a(b|cd)", "x"), "ab acd ac cd", "x x ac cd", id="alternation"),
        pytest.param(CdrewriteArgs("a[bc]", "x"), "ab ac ad a", "x x ad a", id="class"),
        pytest.param(
            CdrewriteArgs("a?b", "x", "l", "r"), "labr lbr laar", "lxr lxr laar",
            id="optional",
        ),
        pytest.param(
            CdrewriteArgs("(ab)+c", "x", "l", "r"), "labcr lababcr lcr", "lxr lxr lcr",
            id="group-plus",
        ),
        pytest.param(
            CdrewriteArgs("(a?)*b", "x", "l", "r"), "lbr laaabr lar", "lxr lxr lar",
            id="nullable-star",
        ),
        pytest.param(CdrewriteArgs("(a|a)b", "(x|x)"), "ab a b", "x a b", id="duplicate-branches"),
        pytest.param(
            CdrewriteArgs("  a\t b\n", " x\t y "), "a b a\tb", "x y a\tb",
            id="expression-whitespace",
        ),
    ],
)
def test_rewrite_io(rule, text, expected):
    assert apply_cdrewrite(text, rule) == expected


@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param("(i|u)", id="two-vowels"),
        pytest.param("i?", id="deletion-or-vowel"),
    ],
)
def test_distinct_equally_weighted_replacements_raise(replacement):
    rule = CdrewriteArgs("ə", replacement)

    with pytest.raises(rewrite.Error, match="Multiple top rewrites"):
        apply_cdrewrite("kət", rule)


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param(("voice", "lenite"), "aɣ", id="first-step-feeds-second"),
        pytest.param(("lenite", "voice"), "ag", id="reverse-step-order"),
    ],
)
def test_normalize_applies_contextual_steps_in_order(steps, expected):
    registry = Registry(
        Symbol=(Token("a"), Token("k"), Token("g"), Token("ɣ"), Pattern("<Word>", "a(g|ɣ)")),
        NormStep=(
            CdrewriteNorm("voice", CdrewriteArgs("k", "g", left_context="a")),
            CdrewriteNorm("lenite", CdrewriteArgs("g", "ɣ", left_context="a")),
        ),
    )
    spec = NormalizationSpec("lenition", steps=steps, licit_patterns=("<Word>",))

    assert normalize("ak", spec, registry) == (expected, "licit", ("<Word>",))


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        pytest.param(None, "aə achche", id="character-atoms"),
        pytest.param(Registry(Symbol=(Token("ch"),)), "achhhe aə", id="inventory-token"),
    ],
)
def test_quantifier_atom_depends_on_inventory(registry, expected):
    rule = CdrewriteArgs("ch+e", "ə", left_context="a")

    assert apply_cdrewrite("achhhe achche", rule, registry) == expected


def test_apply_step_keeps_pattern_definitions_separate_across_registries():
    step = CdrewriteNorm("raise-vowel", CdrewriteArgs("<Target>", "i"))
    first = Registry(Symbol=(Pattern("<Target>", "e"),))
    second = Registry(Symbol=(Pattern("<Target>", "ɛ"),))

    assert apply_step("e ɛ", step, first) == "i ɛ"
    assert apply_step("e ɛ", step, second) == "e i"
    assert apply_step("e ɛ", step, first) == "i ɛ"


def test_apply_step_expands_named_match_and_replacement():
    registry = Registry(
        Symbol=(
            Pattern("<Base>", "(ŋ|ch)"),
            Pattern("<Target>", "<Base>e"),
            Pattern("<Replacement>", "é"),
        ),
    )
    step = CdrewriteNorm("replace", CdrewriteArgs("<Target>", "<Replacement>"))

    assert apply_step("ŋe che xe", step, registry) == "é é xe"


@pytest.mark.parametrize("case", EXCLUSION_CASES, ids=[case["id"] for case in EXCLUSION_CASES])
def test_rewrite_exclusion(case):
    inventory = case["inventory"]
    registry = None if inventory is None else Registry(
        Symbol=tuple(Token(value) for value in inventory)
    )

    assert apply_cdrewrite(case["text"], CdrewriteArgs(**case["rule"]), registry) == case[
        "expected"
    ]


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        pytest.param(CdrewriteArgs("(x", "y"), "x", id="unclosed-match"),
        pytest.param(CdrewriteArgs("x", "y|"), "x", id="incomplete-replacement"),
        pytest.param(CdrewriteArgs("x", "y", "[ab"), "ax", id="unclosed-left-context"),
        pytest.param(CdrewriteArgs("x", "y", right_context="(a|)"), "xa", id="empty-right-branch"),
        pytest.param(CdrewriteArgs("ŋ**", "n"), "ŋ", id="stacked-quantifiers"),
        pytest.param(CdrewriteArgs("ə", "[]"), "ə", id="empty-replacement-class"),
        pytest.param(CdrewriteArgs("t", "d", "|a"), "at", id="empty-left-branch"),
    ],
)
def test_malformed_rule_expression_raises_value_error(rule, text):
    with pytest.raises(ValueError):
        apply_cdrewrite(text, rule)
