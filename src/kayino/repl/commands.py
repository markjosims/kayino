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
    KayinoError,
    NormalizationSpec,
    NormStep,
    Pattern,
    RegexSymbols,
    Registry,
    Text,
    Token,
    TokenGroup,
    resolve_struct_type_fields,
)
from kayino.repl.grammar import parse_command
from kayino.repl.models import NoActiveSpecError, ReplState, get_active_spec
from kayino.repl.styles import TOKEN_STYLES
from kayino.structs import (
    build_struct,
    fetch_struct,
    get_structs,
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

    _, registry = build_struct(
        struct_type_name=struct_type_name,
        registry=state.registry,
        **kwargs,
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
        struct_type_name=struct_type_name,
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
    struct_array = get_structs(struct_type_name, state.registry)
    struct_fields = resolve_struct_type_fields(struct_type_name)
    console = Console()
    with console.capture() as capture:
        table = Table(title=struct_type_name)
        for col in struct_fields:
            table.add_column(col)
        for struct in struct_array:
            table.add_row(*(str(getattr(struct, col, "")) for col in struct_fields))
        console.print(table)

    formatted = to_formatted_text(ANSI(capture.get()))
    return formatted, state


_token_separator = (TOKEN_STYLES["separator"], ":")


def _retrieve_text(
    kwargs: dict,
    registry: Registry,
) -> str:
    input_str = kwargs.get("text", None)
    if input_str is None:
        text_id = kwargs["index"]
        text = fetch_struct(
            struct_type_name=Text.__name__,
            field="id",
            value=text_id,
            registry=registry,
        )
        input_str = text.string
    return input_str


def handle_tokenize(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state.registry)
    formatted_tokens = _tokenize_inner(
        input_str,
        state.registry,
    )
    return formatted_tokens, state


def handle_process(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state.registry)
    spec = get_active_spec(state)
    if spec is None:
        raise NoActiveSpecError
    normalized, status, message = normalize(
        text=input_str,
        spec=spec,
        registry=state.registry,
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
        if not word.strip():
            format_string_tuples.append((" ", None))
        elif isinstance(word, Token):
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
            format_string_tuples.append((TOKEN_STYLES["group"], word.name))

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
    registry = state.registry
    for line in lines:
        _, registry = build_struct(
            struct_type_name=Text.__name__,
            registry=registry,
            source_type="file",
            source_value=filepath,
            string=line,
        )
    state = evolve(
        state,
        registry=registry,
    )

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
        fetch_struct(
            struct_type_name=Pattern.__name__,
            field="name",
            value=kwargs["name"],
            registry=state.registry,
        )
        new_spec = mutate_struct(
            struct=active_spec,
            field=field,
            data=kwargs["name"],
            mutation_type="append",
        )
        status_message = "Appended pattern to spec!"
    elif field == "steps":
        fetch_struct(
            struct_type_name=NormStep.__name__,
            field="name",
            value=kwargs["name"],
            registry=state.registry,
        )
        new_spec = mutate_struct(
            struct=active_spec,
            field=field,
            data=kwargs["name"],
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
    if kwargs["field"] == "licit_patterns":
        new_spec = mutate_struct(
            struct=active_spec,
            field="licit_patterns",
            data=kwargs["name"],
            mutation_type="pop",
        )
        status_message = "Removed pattern from spec!"
    if kwargs["field"] == "steps":
        new_spec = mutate_struct(
            struct=active_spec,
            field="steps",
            data=kwargs["name"],
            mutation_type="pop",
        )
        status_message = "Removed normalization step from spec!"
    else:
        raise ValueError("Uknown field")
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
