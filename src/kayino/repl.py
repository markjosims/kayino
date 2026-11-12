"""
Entrypoint for Kayino REPL.
First goal is to support functionality for adding and removing tokens
and visualizing the tokenization of a string, including which tokens
are OOV.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import prompt_toolkit
import pynini
from attrs import Factory, define, evolve, field, frozen
from frozendict import frozendict
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar, compile
from prompt_toolkit.contrib.regular_languages.completion import GrammarCompleter
from prompt_toolkit.contrib.regular_languages.lexer import GrammarLexer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.styles import Style
from tabulate import tabulate

from kayino.acceptor import tokenize_str
from kayino.models import (
    NAME_TO_STRUCT,
    CdrewriteNorm,
    MappingNorm,
    Pattern,
    RegexSymbols,
    RegistryType,
    Token,
    TokenGroup,
)
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

"""
## Command parsing grammar
TODO: this is getting kinda beefy. Might wanna move to its own submodule.
"""
_struct_name_to_shorthands = defaultdict(list)

for struct_name in NAME_TO_STRUCT.keys():
    _struct_name_to_shorthands[struct_name].append(struct_name.lower())

_struct_name_to_shorthands[Token.__name__].append("tok")
_struct_name_to_shorthands[TokenGroup.__name__].append("tkg")
_struct_name_to_shorthands[Pattern.__name__].append("pat")
_struct_name_to_shorthands[MappingNorm.__name__].append("map")
_struct_name_to_shorthands[CdrewriteNorm.__name__].append("rwr")

_shorthand_to_struct_name = {
    s: k for k, v in _struct_name_to_shorthands.items() for s in v
}


def _regex_union(words: Sequence[str]) -> str:
    return "|".join(words)


_all_struct_names = []
for struct_type_name, shorthands in _struct_name_to_shorthands.items():
    _all_struct_names.extend(shorthands + [struct_type_name])
_all_struct_regex = _regex_union(_all_struct_names)


_struct_builder_patterns = {
    Token.__name__: r"(?P<string>\w+)",
    Pattern.__name__: r"(?P<string>\w+) \s+ (?P<value>\w+)",
    TokenGroup.__name__: r"(?P<string>\w+) (?P<idlist>[\d\s]+)",
    MappingNorm.__name__: r'(?P<name>\w+) \s+ (?P<description>"[^"]+"\s+) '
    r"(?P<relations>(\w+\s+>\w+\s+)+)",
    CdrewriteNorm.__name__: r'(?P<input_str>\w+|"") \s+ (?P<output_str>\w+|"")'
    r" (\s+L:(?P<left_context>\w+))? (\s+R:(?P<right_context>\w+))?",
}

_struct_builder_arg_template = (
    r"((?P<verb>add) \s+ (?P<struct>{struct}) \s+ {struct_args})"
)
_add_struct_patterns = []

for struct_type_name, pattern in _struct_builder_patterns.items():
    shorthand_list = _regex_union(
        _struct_name_to_shorthands[struct_type_name] + [struct_type_name]
    )
    _add_struct_patterns.append(
        _struct_builder_arg_template.format(
            struct=shorthand_list,
            struct_args=pattern,
        )
    )

_add_struct_str = _regex_union(_add_struct_patterns)

_list_struct_str = r"((?P<verb>l(ist)?) \s+ (?P<struct>{struct}))".format(
    struct=_all_struct_regex
)

_struct_builder_arg_template = (
    r"((?P<verb>add) \s+ (?P<struct>{struct}) \s+ {struct_args})"
)
_struct_removal_patterns = {
    Token.__name__: r"(?P<string>[^\d]+)?(?P<id>\d+)?",
    Pattern.__name__: r"(?P<string>\w+)",
    TokenGroup.__name__: r"(?P<string>\w+)",
    MappingNorm.__name__: r"(?P<name>\w+)",
    CdrewriteNorm.__name__: r"(?P<index>\d)",
}

for struct_type_name, pattern in _struct_removal_patterns.items():
    shorthand_list = _regex_union(
        _struct_name_to_shorthands[struct_type_name] + [struct_type_name]
    )
    _add_struct_patterns.append(
        _struct_builder_arg_template.format(
            struct=shorthand_list,
            struct_args=pattern,
        )
    )


def _create_grammar():
    return compile(
        _regex_union(
            (
                _add_struct_str,
                _list_struct_str,
                _remove_struct_str,
            )
        )
    )


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

    grammar = _create_grammar()

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
