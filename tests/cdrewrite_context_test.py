import pytest

from kayino.core import apply_cdrewrite
from kayino.models import CdrewriteArgs, Pattern, Registry, Token


@pytest.mark.parametrize(
    ("left", "right", "text", "expected"),
    [
        pytest.param("a", "", "x ax aax abx", "x ay aay abx", id="left-only"),
        pytest.param("", "b", "x xb xbb xab", "x yb ybb xab", id="right-only"),
        pytest.param("a", "b", "axb ax xb axc cxb", "ayb ax xb axc cxb", id="both-required"),
        pytest.param("ab", "cd", "abxcd bxcd abxc", "abycd bxcd abxc", id="multi-character"),
        pytest.param("a", "a", "axaxa", "ayaya", id="shared-context"),
        pytest.param("(é|ŋ)", "", "éx ŋx ex éx x", "éy ŋy ex éx x", id="unicode-left"),
        pytest.param("", "[é𠮷]", "xé x𠮷 xe x", "yé y𠮷 xe x", id="unicode-right"),
        pytest.param("a", "b?", "axb ax cx", "ayb ay cx", id="nullable-right"),
        pytest.param("(a|é)?", "ŋ", "xŋ éxŋ xk", "yŋ éyŋ xk", id="nullable-left"),
        pytest.param("ŋ+", "", "ŋŋx ŋax ax", "ŋŋy ŋax ax", id="repeated-left"),
        pytest.param(
            "(ab|ŋ)", "(i|u)", "abxi ŋxu bxi abxe", "abyi ŋyu bxi abxe",
            id="alternation-on-both-sides",
        ),
        pytest.param(
            "é", "ŋ", "éxŋ éxŋ exŋ", "éyŋ éxŋ exŋ", id="combining-mark-in-context",
        ),
        pytest.param("y", "", "yxxx xxx", "yyyy xxx", id="left-to-right-feeding"),
        pytest.param("x", "", "xxxx", "xyxy", id="left-context-consumed-by-rewrite"),
        pytest.param("", "x", "xxxx", "yyyx", id="right-context-uses-unprocessed-input"),
        pytest.param("(ŋi)?", "(uŋ)?", "xx ŋixuŋ", "yy ŋiyuŋ", id="both-contexts-nullable"),
        pytest.param("á", "ŋ", "áxŋ á xŋ áx ŋ", "áyŋ á xŋ áx ŋ", id="space-breaks-adjacency"),
    ],
)
def test_rewrite_requires_adjacent_context(left, right, text, expected):
    rule = CdrewriteArgs("x", "y", left, right)

    assert apply_cdrewrite(text, rule) == expected


def test_rewrite_inserts_at_matching_boundary():
    rule = CdrewriteArgs("", "-", left_context="a", right_context="b")

    assert apply_cdrewrite("ab ac cb abab", rule) == "a-b ac cb a-ba-b"


def test_rewrite_inserts_between_multibyte_contexts():
    rule = CdrewriteArgs("", "ə", left_context="ŋ", right_context="é")

    assert apply_cdrewrite("ŋé ŋe né ŋéŋé", rule) == "ŋəé ŋe né ŋəéŋəé"


def test_ambiguous_replacement_is_irrelevant_when_context_blocks_match():
    rule = CdrewriteArgs("ə", "(i|u)", left_context="k", right_context="t")

    assert apply_cdrewrite("kən pət ə", rule) == "kən pət ə"


def test_named_contexts_can_share_a_nested_pattern():
    registry = Registry(
        Symbol=(
            Pattern("<Left>", "<Vowel>ŋ"),
            Pattern("<Right>", "m<Vowel>"),
            Pattern("<Vowel>", "[ai]"),
        ),
    )
    rule = CdrewriteArgs("t", "d", "<Left>", "<Right>")

    assert apply_cdrewrite("aŋtmi iŋtma uŋtmi aŋtmu", rule, registry) == (
        "aŋdmi iŋdma uŋtmi aŋtmu"
    )


def test_deletion_preserves_the_left_context_for_the_next_match():
    rule = CdrewriteArgs("a", "", left_context="ŋ")

    assert apply_cdrewrite("ŋaaa naa", rule) == "ŋ naa"


def test_rewrite_deletes_only_between_contexts():
    rule = CdrewriteArgs("x", "", left_context="a", right_context="b")

    assert apply_cdrewrite("axb ax xb", rule) == "ab ax xb"


def test_rewrite_expands_named_contexts_in_byte_mode():
    registry = Registry(
        Symbol=(
            Token("ch"),
            Pattern("<Left>", "(ch|ŋ)"),
            Pattern("<Right>", "(é|𠮷)"),
        ),
    )
    rule = CdrewriteArgs("x", "y", "<Left>", "<Right>")

    assert apply_cdrewrite("chxé ŋx𠮷 hxé ŋxe chx", rule, registry) == (
        "chyé ŋy𠮷 hxé ŋxe chx"
    )
