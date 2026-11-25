from pathlib import Path
from typing import Literal

from attrs import evolve
from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from rich.console import Console
from rich.table import Table

from kayino.acceptor import surface, tokenize_str
from kayino.core import normalize
from kayino.models import (
    GroupMember,
    KayinoError,
    MappingNorm,
    MappingRow,
    NormalizationSpec,
    NormStep,
    Pattern,
    RegexSymbols,
    Registry,
    SpecPattern,
    SpecStep,
    Text,
    Token,
    TokenGroup,
    resolve_struct_type_fields,
    table_for,
)
from kayino.repl.grammar import parse_command
from kayino.repl.models import NoActiveSpecError, ReplState, get_active_spec
from kayino.repl.styles import TOKEN_STYLES
from kayino.store import (
    ProjectError,
    dump,
    export,
    is_project,
    load,
    validate_project,
)
from kayino.structs import (
    build_struct,
    fetch_struct,
    get_structs,
    group_members,
    mapping,
    next_position,
    remove_struct,
    rename_struct,
    spec_patterns,
    spec_steps,
)


def handle_add(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")

    # Nested args become child rows, inserted after the parent.
    members = ()
    if struct_type_name == TokenGroup.__name__:
        members = kwargs.pop("string_list", None) or ()
    pairs = ()
    if struct_type_name == MappingNorm.__name__:
        pairs = kwargs.pop("mapping", None) or ()

    struct, registry = build_struct(
        struct_type_name=struct_type_name,
        registry=state.registry,
        **kwargs,
    )
    for position, token in enumerate(members):
        _, registry = build_struct(
            struct_type_name=GroupMember.__name__,
            registry=registry,
            group=struct.name,
            position=position,
            token=token,
        )
    for intab, outtab in pairs:
        _, registry = build_struct(
            struct_type_name=MappingRow.__name__,
            registry=registry,
            norm=struct.name,
            intab=intab,
            outtab=outtab,
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


def handle_rename(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    old, new = kwargs["old"], kwargs["new"]
    registry = rename_struct(
        struct_type_name=struct_type_name,
        old_name=old,
        new_name=new,
        registry=state.registry,
    )
    active_spec = state.active_spec
    if table_for(struct_type_name).name == "specs" and active_spec == old:
        active_spec = new
    new_state = evolve(
        state,
        registry=registry,
        active_spec=active_spec,
    )
    return f"Renamed {struct_type_name} {old} to {new}...", new_state


# struct type name -> extra column -> render(registry, struct) -> str
_list_extras = {
    TokenGroup.__name__: {
        "members": lambda registry, group: " ".join(
            group_members(registry, group.name)
        ),
    },
    MappingNorm.__name__: {
        "mapping": lambda registry, norm: "; ".join(
            f"{intab} > {outtab}"
            for intab, outtab in mapping(registry, norm.name).items()
        ),
    },
    NormalizationSpec.__name__: {
        "steps": lambda registry, spec: " ".join(spec_steps(registry, spec.name)),
        "licit_patterns": lambda registry, spec: " ".join(
            spec_patterns(registry, spec.name)
        ),
    },
}


def handle_list(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    struct_array = get_structs(struct_type_name, state.registry)
    struct_fields = resolve_struct_type_fields(struct_type_name)
    # Nested views shown as extra columns, for the row types that have them
    extra_columns = {
        col: render
        for struct in struct_array
        for col, render in _list_extras.get(type(struct).__name__, {}).items()
    }
    if struct_type_name in _list_extras:
        extra_columns |= _list_extras[struct_type_name]
    columns = struct_fields + [col for col in extra_columns if col not in struct_fields]

    def cell(struct: object, col: str) -> str:
        render = _list_extras.get(type(struct).__name__, {}).get(col)
        if render is not None:
            return render(state.registry, struct)
        return str(getattr(struct, col, ""))

    console = Console()
    with console.capture() as capture:
        table = Table(title=struct_type_name)
        for col in columns:
            table.add_column(col)
        for struct in struct_array:
            table.add_row(*(cell(struct, col) for col in columns))
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
        if not surface(word):
            continue
        if not surface(word).strip():
            format_string_tuples.append((" ", None))
        elif isinstance(word, Token):
            if (
                format_string_tuples
                and format_string_tuples[-1][-1] not in RegexSymbols
            ):
                format_string_tuples.append(_token_separator)
            format_string_tuples.append((TOKEN_STYLES["token"], word.name))
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
            field="text",
            value=kwargs["name"],
            registry=state.registry,
        )
        _, new_registry = build_struct(
            struct_type_name=SpecPattern.__name__,
            registry=state.registry,
            spec=active_spec.name,
            pattern=kwargs["name"],
        )
        status_message = "Appended pattern to spec!"
    elif field == "steps":
        fetch_struct(
            struct_type_name=NormStep.__name__,
            field="name",
            value=kwargs["name"],
            registry=state.registry,
        )
        _, new_registry = build_struct(
            struct_type_name=SpecStep.__name__,
            registry=state.registry,
            spec=active_spec.name,
            position=next_position(
                state.registry, "spec_steps", "spec", active_spec.name
            ),
            step=kwargs["name"],
        )
        status_message = "Appended step to spec!"
    else:
        raise ValueError("Invalid field")
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
    # Removes every occurrence of the step/pattern from the spec.
    if kwargs["field"] == "licit_patterns":
        # BUG: same "text" vs "name" grammar mismatch as in handle_spec_add.
        table_name, column = "spec_patterns", "pattern"
        status_message = "Removed pattern from spec!"
    elif kwargs["field"] == "steps":
        table_name, column = "spec_steps", "step"
        status_message = "Removed normalization step from spec!"
    else:
        raise ValueError("Unknown field")
    new_registry = state.registry
    for row in get_structs(table_name, state.registry):
        if row.spec == active_spec.name and getattr(row, column) == kwargs["name"]:
            new_registry = remove_struct(row, new_registry)
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


"""
## Project folders
"""


def _format_violations(header: str, violations) -> str:
    return "\n".join([header] + [f"  {violation}" for violation in violations])


def _path_arg(kwargs: dict[str, str]) -> Path | None:
    path = kwargs.get("path")
    return Path(path.strip()).expanduser() if path and path.strip() else None


def _check_writable_project_dir(path: Path) -> None:
    """Refuse to write into a folder that has other files and is not a project."""
    if path.exists() and not path.is_dir():
        raise ProjectError(f"{path} is not a folder")
    if path.exists() and any(path.iterdir()) and not is_project(path):
        raise ProjectError(f"{path} is not empty and is not a kayino project")


def _save(registry, path: Path) -> str:
    dump(registry, path)
    if violations := validate_project(path):
        return _format_violations(
            f"Saved to {path}, but the saved files have problems:", violations
        )
    return f"Saved to {path}"


def handle_init(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    """
    Create a new project folder from the current session (for a fresh session,
    just the prebuilt norm steps) and make it this session's project.
    """
    path = _path_arg(kwargs)
    if path.exists() and any(path.iterdir()):
        raise ProjectError(f"{path} is not empty")
    message = _save(state.registry, path)
    return message, evolve(state, project_dir=path)


def handle_save(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    path = _path_arg(kwargs) or state.project_dir
    if path is None:
        raise ProjectError("No project folder yet: use `save DIR` or `init DIR`")
    _check_writable_project_dir(path)
    message = _save(state.registry, path)
    return message, evolve(state, project_dir=path)


def handle_load(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    """Replace the session with a project folder's contents."""
    path = _path_arg(kwargs)
    registry = load(path)
    return f"Loaded {path}", ReplState(registry=registry, project_dir=path)


def handle_validate(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    """Validate the project folder on disk, e.g. after editing its CSV files."""
    if state.project_dir is None:
        raise ProjectError("No project folder: use `load DIR` or `save DIR` first")
    if violations := validate_project(state.project_dir):
        return _format_violations(
            f"{state.project_dir} has problems:", violations
        ), state
    return f"{state.project_dir} is valid", state


def handle_export(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    path = _path_arg(kwargs)
    count = export(
        registry=state.registry,
        spec_name=kwargs["spec"],
        out_path=path,
        project_dir=state.project_dir,
    )
    return f"Exported {count} normalized texts to {path}", state


_verb_to_func = {
    "add": handle_add,
    "list": handle_list,
    "remove": handle_remove,
    "rename": handle_rename,
    "tokenize": handle_tokenize,
    "process": handle_process,
    "read": handle_read,
    "spec": handle_spec,
    "init": handle_init,
    "save": handle_save,
    "load": handle_load,
    "validate": handle_validate,
    "export": handle_export,
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
