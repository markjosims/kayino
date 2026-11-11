"""
Entrypoint for Kayino REPL.
First goal is to support functionality for adding and removing tokens
and visualizing the tokenization of a string, including which tokens
are OOV.
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import Sequence

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
from kayino.structs import build_struct, register_struct

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
"""

struct_builder_patterns = {
    Token.__name__: r"(?P<string>\w+)",
    Pattern.__name__: r"(?P<string>\w+) \s+ (?P<value>\w+)",
    TokenGroup.__name__: r"(?P<string>\w+) (?P<idlist>[\d\s]+)",
    MappingNorm.__name__: r'(?P<name>\w+) \s+ (?P<description>"[^"]+"\s+) '
    "(?P<relations>(\w+\s+>\w+\s+)+)",
    CdrewriteNorm.__name__: r'(?P<input_str>\w+|"") \s+ (?P<output_str>\w+|"")'
    " (?:\s+L:(?P<left_context>\w+))? (?:\s+R:(?P<right_context>\w+))?",
}

struct_builder_arg_template = (
    "((?P<verb>add) \s+ (?P<struct>{struct}) \s+ {struct_args})"
)

struct_name_to_shorthands = defaultdict(list)

for struct_name in NAME_TO_STRUCT.keys():
    struct_name_to_shorthands[struct_name].append(struct_name.lower())

struct_name_to_shorthands[Token.__name__].append("tok")
struct_name_to_shorthands[TokenGroup.__name__].append("tkg")
struct_name_to_shorthands[Pattern.__name__].append("pat")
struct_name_to_shorthands[MappingNorm.__name__].append("map")
struct_name_to_shorthands[CdrewriteNorm.__name__].append("rwr")

shorthand_to_struct_name = {
    s: k for k, v in struct_name_to_shorthands.items() for s in v
}


def _regex_union(words: Sequence[str]) -> str:
    return "|".join(words)


_add_struct_patterns = []

for struct_type_name, pattern in struct_builder_patterns.items():
    shorthand_list = _regex_union(struct_name_to_shorthands[struct_type_name])
    _add_struct_patterns.append(
        struct_builder_arg_template.format(
            struct=shorthand_list,
            struct_args=pattern,
        )
    )

_add_struct_str = _regex_union(_add_struct_patterns)
breakpoint()


def _create_grammar():
    return compile(_add_struct_str)


def _prepare_add_kwargs(struct_type_name: str, kwargs: dict):
    # Token and Pattern need no further preparation
    if struct_type_name == TokenGroup.__name__:
        members = map(int, re.findall(r"\b\d\b", kwargs.pop("idlist")))
        members = list(members)
        kwargs["members"] = members
    elif struct_type_name == MappingNorm.__name__:
        pairs = re.findall("(\w+)\s+(\w+)", kwargs.pop("relations"))
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


def handle_add(match, state: ReplState) -> tuple[str, ReplState]:
    kwargs = match.variables()
    struct_shorthand = kwargs["struct"]
    struct_type_name = shorthand_to_struct_name.get(
        struct_shorthand,
        struct_shorthand,
    )
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
    token_table = tabulate(
        ((token.string, token.id) for token in state.registry.tokens),
        headers=("token", "id"),
        tablefmt="github",
    )
    group_table = tabulate(
        (
            (group.string, get_group_members(group, state.registry))
            for group in state.registry[TokenGroup.__name__]
        ),
        headers=("group_id", "members"),
        tablefmt="github",
    )
    tables = f"Tokens:\n{token_table}\nToken groups:\n{group_table}"
    return tables, state


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


def handle_user_response(
    user_response: str,
    state: ReplState,
    grammar: _CompiledGrammar,
) -> tuple[FormattedText | str, ReplState]:
    match = grammar.match(user_response)
    if not match:
        return "Could not parse command!", state
    verb = match.variables()["verb"]
    return _verb_to_func[verb](match, state)


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
            "verb": WordCompleter(_unary_verbs + _binary_verbs + _argless_verbs),
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
            system_response, state = handle_user_response(
                user_response,
                state,
                grammar,
            )
            prompt_toolkit.print_formatted_text(system_response)
    print("GoodBye!")


if __name__ == "__main__":
    repl()
