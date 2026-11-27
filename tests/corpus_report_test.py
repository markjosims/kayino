import pynini
import pytest

from kayino import core
from kayino.models import NormalizationSpec, Pattern, PrebuiltNorm, Registry, Text, Token
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar, parse_command
from kayino.repl.models import ReplState


def test_oov_report_counts_normalized_corpus_after_fsa_error(monkeypatch):
    spec = NormalizationSpec("test", steps=("lowercase",), licit_patterns=("<A>",))
    registry = Registry(
        NormalizationSpec=(spec,),
        NormStep=(PrebuiltNorm("lowercase"),),
        Symbol=(Token("a"), Token("b"), Token("e"), Pattern("<A>", "a")),
    )
    state = ReplState(
        registry=registry,
        active_spec=spec.name,
        texts=[Text(text) for text in ("XX", "E", "A", "B", "xY", "A")],
    )
    fsa = core.fsa

    def compile_text(text, registry):
        if text == "e":
            raise pynini.FstStringCompilationError("test compilation failure")
        return fsa(text, registry)

    monkeypatch.setattr(core, "fsa", compile_text)

    message, returned_state = repl_turn("oov report", state, create_grammar())

    assert message.splitlines() == [
        "Corpus report for test",
        "OOV character occurrences: 4",
        "Distinct OOV characters: 2",
        "Licit texts: 2",
        "Texts with OOV tokens: 2",
        "Texts with FSA compilation errors: 1",
        "Texts with illicit patterns: 1",
    ]
    assert returned_state is state


def test_oov_report_with_empty_corpus():
    spec = NormalizationSpec("test")
    state = ReplState(registry=Registry(NormalizationSpec=(spec,)), active_spec=spec.name)

    message, returned_state = repl_turn("oov report", state, create_grammar())

    assert message.splitlines() == [
        "Corpus report for test",
        "OOV character occurrences: 0",
        "Distinct OOV characters: 0",
        "Licit texts: 0",
        "Texts with OOV tokens: 0",
        "Texts with FSA compilation errors: 0",
        "Texts with illicit patterns: 0",
    ]
    assert returned_state is state


def test_oov_report_requires_active_spec():
    state = ReplState()

    message, returned_state = repl_turn("oov report", state, create_grammar())

    assert message.startswith("NoActiveSpecError:")
    assert returned_state is state


@pytest.mark.parametrize("oov_type", ["text", "token"])
@pytest.mark.parametrize("return_type", ["first", "all"])
def test_existing_oov_commands_still_parse(oov_type, return_type):
    assert parse_command(f"oov {oov_type} {return_type}", create_grammar()) == {
        "verb": "oov",
        "oov_type": oov_type,
        "return_type": return_type,
    }
