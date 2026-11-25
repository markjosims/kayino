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
from kayino.structs import change, delete, fetch, insert, rename, select


def handle_add(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    # Nested args become child rows, inserted with the parent in one change.
    members = kwargs.pop("string_list", None) or () if struct_type_name == TokenGroup.__name__ else ()
    pairs = kwargs.pop("mapping", None) or () if struct_type_name == MappingNorm.__name__ else ()
    with change(state.db) as db:
        insert(db, struct_type_name, **kwargs)
        for position, token in enumerate(members):
            insert(db, GroupMember.__name__, group=kwargs["name"], position=position, token=token)
        for intab, outtab in pairs:
            insert(db, MappingRow.__name__, norm=kwargs["name"], intab=intab, outtab=outtab)
    return f"Added {struct_type_name} to registry...", state


def handle_remove(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    with change(state.db) as db:
        delete(db, struct_type_name, **kwargs)
    return f"Removed {struct_type_name} from registry...", state


def handle_rename(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    old, new = kwargs["old"], kwargs["new"]
    rename(state.db, struct_type_name, old, new)
    if table_for(struct_type_name).name == "specs" and state.active_spec == old:
        state = evolve(state, active_spec=new)
    return f"Renamed {struct_type_name} {old} to {new}...", state


# struct type name -> extra column -> render(db, struct) -> str
_list_extras = {
    TokenGroup.__name__: {
        "members": lambda db, group: " ".join(
            m.token for m in select(db, GroupMember.__name__, group=group.name)
        ),
    },
    MappingNorm.__name__: {
        "mapping": lambda db, norm: "; ".join(
            f"{r.intab} > {r.outtab}" for r in select(db, MappingRow.__name__, norm=norm.name)
        ),
    },
    NormalizationSpec.__name__: {
        "steps": lambda db, spec: " ".join(
            s.step for s in select(db, SpecStep.__name__, spec=spec.name)
        ),
        "licit_patterns": lambda db, spec: " ".join(
            p.pattern for p in select(db, SpecPattern.__name__, spec=spec.name)
        ),
    },
}


def handle_list(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    struct_array = select(state.db, struct_type_name)
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
            return render(state.db, struct)
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
    db: Registry,
) -> str:
    input_str = kwargs.get("text", None)
    if input_str is None:
        text_id = kwargs["index"]
        input_str = fetch(db, Text.__name__, id=text_id).string
    return input_str


def handle_tokenize(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state.db)
    return _tokenize_inner(input_str, state.db), state


def handle_process(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state.db)
    spec = get_active_spec(state)
    if spec is None:
        raise NoActiveSpecError
    normalized, status, message = normalize(input_str, spec, state.db)
    return _tokenize_inner(normalized, state.db), state


def _tokenize_inner(input_str: str, db: Registry) -> FormattedText:
    token_list = tokenize_str(input_str, select(db, "symbols"))
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
    with change(state.db) as db:
        for line in lines:
            insert(db, Text.__name__, string=line, source_type="file", source_value=filepath)
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
    with change(state.db) as db:
        if field == "licit_patterns":
            fetch(db, Pattern.__name__, text=kwargs["name"])
            insert(db, SpecPattern.__name__, spec=active_spec.name, pattern=kwargs["name"])
            status_message = "Appended pattern to spec!"
        elif field == "steps":
            (position,) = db.execute(
                'SELECT COALESCE(MAX("position") + 1, 0) FROM "spec_steps" WHERE "spec" = ?',
                (active_spec.name,),
            ).fetchone()
            insert(db, SpecStep.__name__, spec=active_spec.name, position=position, step=kwargs["name"])
            status_message = "Appended step to spec!"
        else:
            raise ValueError("Invalid field")
    return status_message, state


def handle_spec_remove(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec,
) -> tuple[FormattedText | str, ReplState]:
    # Removes every occurrence of the step/pattern from the spec.
    if kwargs["field"] == "licit_patterns":
        # BUG: same "text" vs "name" grammar mismatch as in handle_spec_add.
        type_name, column = SpecPattern.__name__, "pattern"
        status_message = "Removed pattern from spec!"
    elif kwargs["field"] == "steps":
        type_name, column = SpecStep.__name__, "step"
        status_message = "Removed normalization step from spec!"
    else:
        raise ValueError("Unknown field")
    with change(state.db) as db:
        delete(db, type_name, spec=active_spec.name, **{column: kwargs["name"]})
    return status_message, state


def handle_spec_activate(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec | None,
) -> tuple[FormattedText | str, ReplState]:
    if spec_name := kwargs.get("name", None):
        spec = fetch(state.db, NormalizationSpec.__name__, name=spec_name)
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


def _save(db: Registry, path: Path) -> str:
    dump(db, path)
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
    message = _save(state.db, path)
    return message, evolve(state, project_dir=path)


def handle_save(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    path = _path_arg(kwargs) or state.project_dir
    if path is None:
        raise ProjectError("No project folder yet: use `save DIR` or `init DIR`")
    _check_writable_project_dir(path)
    message = _save(state.db, path)
    return message, evolve(state, project_dir=path)


def handle_load(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    """Replace the session with a project folder's contents."""
    path = _path_arg(kwargs)
    return f"Loaded {path}", ReplState(db=load(path), project_dir=path)


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
        db=state.db,
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
