from pathlib import Path

from attrs import evolve
from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar
from prompt_toolkit.formatted_text import FormattedText

from kayino.acceptor import tokenize_str
from kayino.models import (
    NormalizationSpec,
    NormStep,
    Pattern,
    RegexSymbols,
    Text,
    Token,
    TokenGroup,
)
from kayino.repl.grammar import parse_command
from kayino.repl.models import ReplState, get_active_spec
from kayino.repl.styles import TOKEN_STYLES
from kayino.structs import (
    FsaCompilationError,
    MalformedStructArrayError,
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
    return str(state.registry[struct_type_name]), state


_token_separator = (TOKEN_STYLES["separator"], ":")


def handle_tokenize(
    kwargs: dict[str, str],
    state: ReplState,
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
        new_registry = register_struct(state.registry, struct)
        state = evolve(state, registry=new_registry)

    return f"Read {len(lines)} texts...", state


def handle_spec(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    subverb = kwargs.pop("subverb")
    active_spec = get_active_spec(state)
    if not active_spec and not ((subverb == "activate") and ("name" in kwargs)):
        raise ValueError("No active normalization spec!")
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
    if not len(kwargs) == 1:
        raise ValueError
    if pattern_text := kwargs.get("text", None):
        new_spec = mutate_struct(
            struct=active_spec,
            field="licit_patterns",
            data=pattern_text,
            mutation_type="append",
            registry=state.registry,
        )
        status_message = "Appended pattern to spec!"
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
            mutation_type="append",
        )
        status_message = "Appended normalization step to spec!"
    new_registry = update_struct(
        old_stuct=active_spec,
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
        old_stuct=active_spec,
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
            active_spec=spec,
        )
        return f"Set active spec to {spec_name}", new_state
    return f"Current active spec: {active_spec}", state


_verb_to_func = {
    "add": handle_add,
    "list": handle_list,
    "remove": handle_remove,
    "tokenize": handle_tokenize,
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
    except FsaCompilationError as e:
        return f"Could not compile FSA! {e}", state
    except MalformedStructArrayError as e:
        return f"Invalid input! {e}", state
