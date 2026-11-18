from pathlib import Path

from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar
from prompt_toolkit.formatted_text import FormattedText

from kayino.acceptor import tokenize_str
from kayino.models import Pattern, RegexSymbols, Text, Token, TokenGroup
from kayino.repl.grammar import parse_command
from kayino.repl.models import ReplState
from kayino.repl.styles import TOKEN_STYLES
from kayino.structs import (
    FsaCompilationError,
    build_struct,
    fetch_struct,
    register_struct,
    remove_struct,
)


def handle_add(kwargs: dict[str, str], state: ReplState) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")

    struct = build_struct(
        struct_type_name=struct_type_name,
        registry=state.registry,
        **kwargs,
    )
    registry = register_struct(
        registry=state.registry,
        struct=struct,
    )
    state.registry = registry
    return f"Added {struct_type_name} to registry...", state


def handle_remove(kwargs: dict[str, str], state: ReplState) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    assert len(kwargs) == 1
    field, value = kwargs.popitem()
    struct = fetch_struct(
        field=field,
        value=value,
        registry=state.registry,
    )
    registry = remove_struct(struct, state.registry)
    state.registry = registry
    return f"Removed {struct_type_name} from registry...", state


def handle_list(kwargs: dict[str, str], state: ReplState) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    return str(state.registry[struct_type_name]), state


_token_separator = (TOKEN_STYLES["separator"], ":")


def handle_tokenize(
    kwargs: dict[str, str], state: ReplState
) -> tuple[FormattedText | str, ReplState]:
    input_str = kwargs["text"]
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
        elif isinstance(word, (TokenGroup, Pattern)):
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


def handle_read(
    kwargs: dict[str, str], state: ReplState
) -> tuple[FormattedText | str, ReplState]:
    filepath = kwargs["text"]
    lines = Path(filepath).read_text().splitlines()
    structs = [
        build_struct(
            Text.__name__,
            state.registry,
            source_type="file",
            source_value=filepath,
            string=line,
        )
        for line in lines
    ]
    for struct in structs:
        state.registry = register_struct(state.registry, struct)
    return f"Read {len(lines)} texts...", state


_verb_to_func = {
    "add": handle_add,
    "list": handle_list,
    "remove": handle_remove,
    "tokenize": handle_tokenize,
    "read": handle_read,
}


def repl_turn(
    user_response: str,
    state: ReplState,
    grammar: _CompiledGrammar,
) -> tuple[FormattedText | str, ReplState]:
    try:
        kwargs = parse_command(user_response, grammar)
        verb = kwargs.pop("verb")
    except Exception as e:
        return f"Could not parse command! {e}", state
    try:
        return _verb_to_func[verb](kwargs, state)
    except FsaCompilationError as e:
        return f"Could not compile FSA! {e}", state
