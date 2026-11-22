from pathlib import Path
from typing import Literal

from attrs import evolve
from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from rich.console import Console
from rich.table import Table

from kayino.acceptor import tokenize_str
from kayino.core import normalize
from kayino.models import (
    NAME_TO_STRUCT,
    TYPE_TO_UNION,
    KayinoError,
    NormalizationSpec,
    NormStep,
    Pattern,
    RegexSymbols,
    Registry,
    Text,
    Token,
    TokenGroup,
)
from kayino.repl.grammar import parse_command
from kayino.repl.models import NoActiveSpecError, ReplState, get_active_spec
from kayino.repl.styles import TOKEN_STYLES
from kayino.structs import (
    build_struct,
    fetch_struct,
    mutate_struct,
    register_struct,
    remove_struct,
    update_struct,
)


def handle_add(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
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
    new_state = evolve(
        state,
        registry=registry,
    )
    return f"Added {struct_type_name} to registry...", new_state


def handle_remove(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    assert len(kwargs) == 1
    field, value = kwargs.popitem()
    struct = fetch_struct(
        field=field,
        value=value,
        registry=state.registry,
    )
    registry = remove_struct(struct, state.registry)
    new_state = evolve(
        state,
        registry=registry,
    )
    return f"Removed {struct_type_name} from registry...", new_state


def handle_list(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    if union_type := TYPE_TO_UNION.get(struct_type_name, None):
        union_structs = state.registry[union_type]
        struct_array = tuple(
            filter(
                (lambda s: type(s).__name__ == struct_type_name),
                union_structs,
            )
        )

    struct_array = state.registry[struct_type_name]

    struct_type = NAME_TO_STRUCT[struct_type_name]
    struct_attrs = struct_type.__attrs_attrs__
    attr_cols = [struct_attr.name for struct_attr in struct_attrs]
    console = Console()
    with console.capture() as capture:
        table = Table(title=struct_type_name)
        for col in attr_cols:
            table.add_column(col)
        for struct in struct_array:
            table.add_row(*(str(getattr(struct, col)) for col in attr_cols))
        console.print(table)

    formatted = to_formatted_text(ANSI(capture.get()))
    return formatted, state


_token_separator = (TOKEN_STYLES["separator"], ":")


def handle_tokenize(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = kwargs["text"]
    formatted_tokens = _tokenize_inner(
        input_str,
        state.registry,
    )
    return formatted_tokens, state


def handle_process(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = kwargs["text"]
    spec = get_active_spec(state)
    normalized, status, message = normalize(
        text=input_str,
        spec=spec,
    )
    formatted_tokens = _tokenize_inner(
        normalized,
        state.registry,
    )
    return formatted_tokens, state


def _tokenize_inner(input_str: str, registry: Registry) -> FormattedText:
    token_list = tokenize_str(input_str, registry)
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
    return FormattedText(format_string_tuples)


def handle_read(
    kwargs: dict[str, str],
    state: ReplState,
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
        new_registry = register_struct(
            registry=state.registry,
            struct=struct,
        )
        state = evolve(state, registry=new_registry)

    return f"Read {len(lines)} texts...", state


def handle_spec(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    subverb = kwargs.pop("subverb")
    active_spec = get_active_spec(state)
    if not active_spec and not ((subverb == "activate") and ("name" in kwargs)):
        raise NoActiveSpecError
    if subverb == "add":
        return handle_spec_add(
            kwargs=kwargs,
            state=state,
            active_spec=active_spec,
        )
    elif subverb == "remove":
        return handle_spec_remove(
            kwargs=kwargs,
            state=state,
            active_spec=active_spec,
        )
    elif subverb == "activate":
        return handle_spec_activate(
            kwargs=kwargs,
            state=state,
            active_spec=active_spec,
        )
    raise ValueError("Unknown subcommand for verb `spec`!")


def handle_spec_add(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec,
) -> tuple[FormattedText | str, ReplState]:
    field: Literal["steps", "licit_patterns"] = kwargs["field"]
    if field == "licit_patterns":
        new_spec = mutate_struct(
            struct=active_spec,
            field=field,
            data=kwargs["name"],
            mutation_type="append",
        )
        status_message = "Appended step to spec!"
    elif field == "steps":
        step = fetch_struct(
            struct_type_name=NormStep.__name__,
            field="name",
            value=kwargs["name"],
            registry=state.registry,
        )
        new_spec = mutate_struct(
            struct=active_spec,
            field=field,
            data=step,
            mutation_type="append",
        )
        status_message = "Appended step to spec!"
    else:
        raise ValueError("Invalid field")
    new_registry = update_struct(
        old_struct=active_spec,
        new_struct=new_spec,
        registry=state.registry,
    )
    new_state = evolve(
        state,
        registry=new_registry,
    )
    return status_message, new_state


def handle_spec_remove(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec,
) -> tuple[FormattedText | str, ReplState]:
    if not len(kwargs) == 1:
        raise ValueError
    if pattern_text := kwargs.get("text", None):
        new_spec = mutate_struct(
            struct=active_spec,
            field="licit_patterns",
            data=pattern_text,
            mutation_type="remove",
            registry=state.registry,
        )
        status_message = "Removed pattern from spec!"
    elif step_name := kwargs.get("name", None):
        step = fetch_struct(
            struct_type_name=NormStep.__name__,
            field="name",
            value=step_name,
            registry=state.registry,
        )
        new_spec = mutate_struct(
            struct=active_spec,
            field="steps",
            data=step,
            mutation_type="remove",
        )
        status_message = "Removed normalization step from spec!"
    new_registry = update_struct(
        old_struct=active_spec,
        new_struct=new_spec,
        registry=state.registry,
    )
    new_state = evolve(
        state,
        registry=new_registry,
    )
    return status_message, new_state


def handle_spec_activate(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec | None,
) -> tuple[FormattedText | str, ReplState]:
    if spec_name := kwargs.get("name", None):
        spec = fetch_struct(
            struct_type_name=NormalizationSpec.__name__,
            field="name",
            value=spec_name,
            registry=state.registry,
        )
        new_state = evolve(
            state,
            active_spec=spec.name,
        )
        return f"Set active spec to {spec_name}", new_state
    return f"Current active spec: {active_spec}", state


_verb_to_func = {
    "add": handle_add,
    "list": handle_list,
    "remove": handle_remove,
    "tokenize": handle_tokenize,
    "process": handle_process,
    "read": handle_read,
    "spec": handle_spec,
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
    except KayinoError as e:
        return f"{type(e).__name__}: {e}", state
