"""
Entrypoint for Kayino REPL.
First goal is to support functionality for adding and removing tokens
and visualizing the tokenization of a string, including which tokens
are OOV.
"""

from pathlib import Path
from typing import Sequence

import prompt_toolkit
import pynini
from attrs import Factory, evolve, frozen
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
from kayino.models import RegistryType, Token, TokenGroup

_state_cache_dir = Path(".cache")
_state_cache_dir.mkdir(exist_ok=True)

_white = "#ffffff"
_green = "#33aa33"
_red = "#ff0000"
_blue = "#0000ff"
_purple = "#800080"
_black = "#ffffff"
_maroon = "#662222"
_yellow = "#FFBF00"


@frozen
class ReplState:
    texts: dict[str, str] = Factory(dict)
    registry: RegistryType


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


def _add_token(match, state: ReplState) -> tuple[str, ReplState]:
    token_string = match.variables()["arg"]
    token, new_registry = register_token(token_string, state.registry)
    state = evolve(
        state,
        registry=new_registry,
    )
    return f"New token {token.string} at index {token.id}", state


def _add_token_group(match, state: ReplState) -> tuple[str, ReplState]:
    group_string = match.variables()["arg1"]
    member_str = match.variables()["arg2"]
    members = tuple(map(int, member_str.split()))
    id_map = get_token_id_map(state.registry.tokens)
    member_strings = [id_map[member_id].string for member_id in members]
    new_registry = register_token_group(
        group_string,
        members,
        state.registry,
    )
    state = evolve(
        state,
        registry=new_registry,
    )
    return f"New token group {group_string} with members {member_strings}", state


def _remove_token_group(match, state: ReplState) -> tuple[str, ReplState]:
    group_string = match.variables()["arg"]
    new_registry = remove_token_group(
        group_string,
        state.registry,
    )
    state = evolve(
        state,
        registry=new_registry,
    )

    return f"Removed token group {group_string}", state


def _remove_token(match, state: ReplState) -> tuple[FormattedText | str, ReplState]:
    token_string = match.variables()["arg"]
    if token := get_token_string_map(state.registry.tokens).get(token_string):
        token_id = token.id
    else:
        try:
            token_id = int(token_string.strip())
            token_string = get_token_id_map(state.registry.tokens)[token_id]
        except ValueError:
            return "Unrecognized token!", state
        except KeyError:
            return "Token out of range!", state
    removed_token_id, new_registry = remove_token(token_id, state.registry)
    if removed_token_id != token_id:
        raise ValueError(
            f"Requested removal of token {token_id} but got {removed_token_id}"
        )
    new_state = evolve(
        state,
        registry=new_registry,
    )
    return f"Removed token at index {token_id} with string {token_string}", new_state


def _list_tokens(match, state: ReplState) -> tuple[FormattedText | str, ReplState]:
    token_table = tabulate(
        ((token.string, token.id) for token in state.registry.tokens),
        headers=("token", "id"),
        tablefmt="github",
    )
    group_table = tabulate(
        (
            (group.string, get_group_members(group, state.registry))
            for group in state.registry.token_groups
        ),
        headers=("group_id", "members"),
        tablefmt="github",
    )
    tables = f"Tokens:\n{token_table}\nToken groups:\n{group_table}"
    return tables, state


_token_separator = (TOKEN_STYLES["separator"], ":")


def _proc_str(match, state: ReplState) -> tuple[FormattedText | str, ReplState]:
    input_str = match.variables()["arg"]
    token_list = tokenize_str(input_str, state.registry)
    format_string_tuples = []
    for word in token_list:
        if not word.string:
            continue
        if isinstance(word, Token):
            if (
                format_string_tuples
                and format_string_tuples[-1][0] != TOKEN_STYLES["operator"]
            ):
                format_string_tuples.append(_token_separator)
            format_string_tuples.append((TOKEN_STYLES["token"], word.string))
        elif isinstance(word, TokenGroup):
            if (
                format_string_tuples
                and format_string_tuples[-1][0] != TOKEN_STYLES["operator"]
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


_unary_verbs = [
    "add",
    "remove",
    "proc",
    "remove_group",
]
_binary_verbs = [
    "add_group",
]
_argless_verbs = [
    "list",
]

_verb_to_func = {
    "add": _add_token,
    "remove": _remove_token,
    "list": _list_tokens,
    "proc": _proc_str,
    "add_group": _add_token_group,
    "remove_group": _remove_token_group,
}


def _regex_union(words: Sequence[str]) -> str:
    return "|".join(words)


def _create_grammar():
    return compile(
        # unary verbs: verb arg
        rf"(\s* (?P<verb>{_regex_union(_unary_verbs)})"
        r"\s+ (?P<arg>.+)"
        r"\s* ) |"
        # binary verbs: verb arg1 arg2
        # ATM only binary verb is add_group
        # where arg1 must be surrounded in angle brackets
        # and arg2 is a list of indices
        rf"(\s* (?P<verb>{_regex_union(_binary_verbs)})"
        r"\s+ (?P<arg1>\<\w+\>)"
        r"\s+ (?P<arg2>[\d\s]+)"
        r"\s* ) |"
        # argless verbs: verb
        rf"(\s* (?P<verb>{_regex_union(_argless_verbs)})"
        r"\s* )"
    )


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
