"""
Entrypoint for Kayino REPL.
First goal is to support functionality for adding and removing tokens
and visualizing the tokenization of a string, including which tokens
are OOV.
"""

import re
from collections import defaultdict
from pathlib import Path

import prompt_toolkit
from attrs import Factory, define
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar
from prompt_toolkit.contrib.regular_languages.completion import GrammarCompleter
from prompt_toolkit.contrib.regular_languages.lexer import GrammarLexer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.styles import Style

from kayino.acceptor import tokenize_str
from kayino.models import (
    CdrewriteNorm,
    MappingNorm,
    RegexSymbols,
    RegistryType,
    Token,
    TokenGroup,
)
from kayino.repl.grammar import create_grammar
from kayino.structs import FsaCompilationError, build_struct, register_struct

_state_cache_dir = Path(".cache")
_state_cache_dir.mkdir(exist_ok=True)

"""
## Text formatting styles
"""

_white = "#ffffff"
_green = "#33aa33"
_red = "#ff0000"
_blue = "#0000ff"
_purple = "#800080"
_black = "#ffffff"
_maroon = "#662222"
_yellow = "#FFBF00"


@define
class ReplState:
    registry: RegistryType = Factory(lambda: defaultdict(list))
    texts: dict[str, str] = Factory(dict)


TOKEN_STYLES = {
    "operator": f"{_blue} bold",
    "token": f"{_purple} bold",
    "unrecognized": f"bg:{_maroon} {_black}",
    "separator": _white,
    "group": _yellow,
}
INPUT_STYLES = {
    "verb": f"{_green} bold",
    "arg": _white,
}


def _prepare_add_kwargs(struct_type_name: str, kwargs: dict):
    # Token and Pattern need no further preparation
    if struct_type_name == TokenGroup.__name__:
        members = map(int, re.findall(r"\b\d\b", kwargs.pop("idlist")))
        members = list(members)
        kwargs["members"] = members
    elif struct_type_name == MappingNorm.__name__:
        pairs = re.findall(r"(\w+)\s+(\w+)", kwargs.pop("relations"))
        data = dict(pairs)
        kwargs["data"] = data
        kwargs["kind"] = "mapping"
    elif struct_type_name == CdrewriteNorm.__name__:
        data = {}
        for kwarg in [
            "input_str",
            "output_str",
            "left_context",
            "right_context",
        ]:
            data[kwarg] = kwargs.pop(kwarg)
        kwargs["data"] = data
        kwargs["kind"] = "cdrewrite"
    return kwargs


def _kwargs_from_match(match) -> dict:
    kwargs = {kwarg.varname: kwarg.value for kwarg in match.variables()}
    return kwargs


def _pop_struct_type_name_from_kwargs(kwargs: dict) -> tuple[str, dict]:
    struct_shorthand = kwargs.pop("struct")
    struct_type_name = _shorthand_to_struct_name.get(
        struct_shorthand,
        struct_shorthand,
    )
    return struct_type_name, kwargs


def handle_add(match, state: ReplState) -> tuple[str, ReplState]:
    kwargs = _kwargs_from_match(match)
    struct_type_name, kwargs = _pop_struct_type_name_from_kwargs(kwargs)

    struct_kwargs = _prepare_add_kwargs(struct_type_name, kwargs)
    struct = build_struct(
        struct_type_name=struct_type_name,
        registry=state.registry,
        **struct_kwargs,
    )
    registry = register_struct(
        registry=state.registry,
        struct=struct,
    )
    state.registry = registry
    return f"Added {struct_type_name} to registry...", state


def handle_list(match, state: ReplState) -> tuple[str, ReplState]:
    kwargs = _kwargs_from_match(match)
    struct_type_name, kwargs = _pop_struct_type_name_from_kwargs(kwargs)
    return str(state.registry[struct_type_name]), state


_token_separator = (TOKEN_STYLES["separator"], ":")


def handle_proc(match, state: ReplState) -> tuple[FormattedText | str, ReplState]:
    input_str = match.variables()["arg"]
    token_list = tokenize_str(input_str, state.registry)
    format_string_tuples = []
    for word in token_list:
        if not word.string:
            continue
        if isinstance(word, Token):
            if (
                format_string_tuples
                and format_string_tuples[-1][-1] not in RegexSymbols
            ):
                format_string_tuples.append(_token_separator)
            format_string_tuples.append((TOKEN_STYLES["token"], word.string))
        elif isinstance(word, TokenGroup):
            if (
                format_string_tuples
                and format_string_tuples[-1][-1] not in RegexSymbols
            ):
                format_string_tuples.append(_token_separator)
            format_string_tuples.append((TOKEN_STYLES["group"], word.string))

        else:
            if (
                word.kind != "operator"
                and format_string_tuples
                and format_string_tuples[-1][0] != TOKEN_STYLES["operator"]
            ):
                format_string_tuples.append(_token_separator)
            format_string_tuples.append((TOKEN_STYLES[word.kind], word.string))

    return FormattedText(format_string_tuples), state


_verb_to_func = {
    "add": handle_add,
    "list": handle_list,
    "proc": handle_proc,
}


def repl_turn(
    user_response: str,
    state: ReplState,
    grammar: _CompiledGrammar,
) -> tuple[FormattedText | str, ReplState]:
    match = grammar.match(user_response)
    if not match:
        return "Could not parse command!", state
    verb = match.variables()["verb"]
    try:
        return _verb_to_func[verb](match, state)
    except FsaCompilationError as e:
        return f"Could not compile FSA! {e}", state


def repl():
    session = prompt_toolkit.PromptSession()
    state = ReplState()

    grammar = create_grammar()

    lexer = GrammarLexer(
        grammar,
        lexers={
            "verb": SimpleLexer("class:verb"),
            "arg": SimpleLexer("class:arg"),
        },
    )

    completer = GrammarCompleter(
        grammar,
        {
            "verb": WordCompleter(["add"]),
        },
    )
    input_style = Style.from_dict(INPUT_STYLES)

    while True:
        try:
            user_response = session.prompt(
                "kyn: ",
                lexer=lexer,
                completer=completer,
                style=input_style,
            )
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        else:
            system_response, state = repl_turn(
                user_response,
                state,
                grammar,
            )
            prompt_toolkit.print_formatted_text(system_response)
    print("GoodBye!")


if __name__ == "__main__":
    repl()
