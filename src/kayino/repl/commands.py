import json
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from attrs import asdict, evolve
from cattrs import structure
from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

from kayino.acceptor import tokenize_str
from kayino.constants import MAX_OUTPUT_STRINGS
from kayino.core import apply_step, normalize, summarize_corpus
from kayino.models import (
    KayinoError,
    MappingNorm,
    NormalizationSpec,
    NormalizationStatus,
    NormStep,
    Pattern,
    RegexSymbols,
    Registry,
    StateLoadError,
    StateSaveError,
    Text,
    Token,
    get_structs,
    resolve_struct_type_fields,
)
from kayino.prebuilt import universal_preproc
from kayino.repl.grammar import parse_command
from kayino.repl.models import NoActiveSpecError, ReplState, get_active_spec
from kayino.repl.styles import TOKEN_STYLES
from kayino.structs import (
    build_struct,
    fetch_struct,
    mutate_struct,
    put_struct,
    remove_struct,
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


def _rich_table(
    title: str,
    cols: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> FormattedText:
    console = Console()
    with console.capture() as capture:
        table = Table(title=title)
        for col in cols:
            table.add_column(col)
        for row in rows:
            table.add_row(*row)
        console.print(table)

    formatted = to_formatted_text(ANSI(capture.get()))
    return formatted


def handle_list(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[str, ReplState]:
    struct_type_name = kwargs.pop("struct")
    struct_array = get_structs(struct_type_name, state.registry)
    struct_fields = resolve_struct_type_fields(struct_type_name)
    rows = []
    for struct in struct_array:
        rows.append([str(getattr(struct, col, "")) for col in struct_fields])
    formatted = _rich_table(
        title=struct_type_name,
        cols=struct_fields,
        rows=rows,
    )
    return formatted, state


_token_separator = (TOKEN_STYLES["separator"], ":")


def _retrieve_text(
    kwargs: dict,
    state: ReplState,
) -> str:
    input_str = kwargs.get("text", None)
    if input_str is None:
        i = kwargs["index"]
        text = state.texts[i]
        input_str = text.string
    return input_str


def handle_tokenize(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state)
    formatted_tokens = _tokenize_inner(
        input_str,
        state.registry,
    )
    return formatted_tokens, state


def handle_process(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state)
    spec = get_active_spec(state)
    if spec is None:
        raise NoActiveSpecError
    normalized, status, status_payload = normalize(
        text=input_str,
        spec=spec,
        registry=state.registry,
    )
    formatted_tokens = _tokenize_inner(
        normalized,
        state.registry,
    )
    status_message = _status_message(
        status=status,
        status_payload=status_payload,
    )
    result_message = formatted_tokens + [("", "\n"), status_message]
    result_message = FormattedText(result_message)
    return result_message, state


def _status_message(
    status: NormalizationStatus,
    status_payload: tuple[str, ...] | None,
) -> tuple[str, ...]:
    if status == "licit":
        return (
            "",
            "Valid patterns:\n\t" + "\n\t".join(status_payload),
        )
    elif status == "illicit_token":
        return (
            "",
            "Invalid tokens\n\t" + "\n\t".join(status_payload),
        )
    return (
        "",
        "No pattern matched!",
    )


def handle_apply(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    input_str = _retrieve_text(kwargs, state)
    step = fetch_struct(
        struct_type_name=NormStep.__name__,
        field="name",
        value=kwargs["name"],
        registry=state.registry,
    )
    result = apply_step(
        step=step,
        text=input_str,
    )

    formatted_tokens = _tokenize_inner(
        result,
        state.registry,
    )
    return formatted_tokens, state


def _tokenize_inner(input_str: str, registry: Registry) -> FormattedText:
    token_list = tokenize_str(input_str, registry)
    format_string_tuples = []
    for word in token_list:
        if not word.string:
            continue
        if not word.string.strip():
            format_string_tuples.append(("", ""))
        elif isinstance(word, Token):
            if (
                format_string_tuples
                and format_string_tuples[-1][-1] not in RegexSymbols
            ):
                format_string_tuples.append(_token_separator)
            format_string_tuples.append((TOKEN_STYLES["token"], word.string))
        elif isinstance(word, Pattern):
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
    texts = [
        Text(
            string=universal_preproc(line),
            source_type="file",
            source_value=filepath,
        )
        for line in lines
    ]
    new_text_array = state.texts + texts
    state = evolve(
        state,
        texts=new_text_array,
    )

    return f"Read {len(lines)} texts...", state


def handle_map(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    subverb = kwargs.pop("subverb")
    name = kwargs.pop("name")
    map_step = fetch_struct(
        struct_type_name=MappingNorm.__name__,
        field="name",
        value=name,
        registry=state.registry,
    )
    if subverb == "append":
        pair = (kwargs["intab"], kwargs["outtab"])
        new_map = mutate_struct(
            struct=map_step,
            field="data",
            data=pair,
            mutation_type="append",
        )
        status = f"Inserted {pair} to map"
    elif subverb == "pop":
        key = kwargs["intab"]
        new_map = mutate_struct(
            struct=map_step,
            field="data",
            data=key,
            mutation_type="pop",
        )
        status = f"Popped {key} from map"
    else:
        raise ValueError
    registry = put_struct(
        old_struct=map_step,
        new_struct=new_map,
        registry=state.registry,
    )
    state = evolve(state, registry=registry)
    return status, state


def handle_spec(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    subverb = kwargs.pop("subverb")
    active_spec = get_active_spec(state)
    if not active_spec and not ((subverb == "activate") and ("name" in kwargs)):
        raise NoActiveSpecError
    if subverb == "append":
        return handle_spec_append(
            kwargs=kwargs,
            state=state,
            active_spec=active_spec,
        )
    elif subverb == "pop":
        return handle_spec_pop(
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


def handle_spec_append(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec,
) -> tuple[FormattedText | str, ReplState]:
    field: Literal["steps", "licit_patterns"] = kwargs["field"]
    if field == "licit_patterns":
        # fetch to check for existence
        # but don't store result since only name is stored
        fetch_struct(
            struct_type_name=Pattern.__name__,
            field="string",
            value=kwargs["string"],
            registry=state.registry,
        )
        new_spec = mutate_struct(
            struct=active_spec,
            field=field,
            data=kwargs["string"],
            mutation_type="append",
        )
        status_message = "Appended pattern to spec!"
    elif field == "steps":
        # ditto re: fetching
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
    new_registry = put_struct(
        old_struct=active_spec,
        new_struct=new_spec,
        registry=state.registry,
    )
    new_state = evolve(
        state,
        registry=new_registry,
    )
    return status_message, new_state


def handle_spec_pop(
    kwargs: dict[str, str],
    state: ReplState,
    active_spec: NormalizationSpec,
) -> tuple[FormattedText | str, ReplState]:
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
    new_registry = put_struct(
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


def handle_oov(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    if "report" in kwargs:
        spec = get_active_spec(state)
        if spec is None:
            raise NoActiveSpecError

        report = summarize_corpus(
            (text.string for text in state.texts),
            spec,
            state.registry,
        )
        return (
            f"Corpus report for {spec.name}\n"
            f"OOV character occurrences: {report.oov_occurrences}\n"
            f"Distinct OOV characters: {report.distinct_oov_characters}\n"
            f"Licit texts: {report.licit}\n"
            f"Texts with OOV tokens: {report.illicit_token}\n"
            f"Texts with FSA compilation errors: {report.compilation_error}\n"
            f"Texts with illicit patterns: {report.illicit_pattern}",
            state,
        )

    processed = _process_all_texts(state, start=kwargs.get("index", 0))

    oov_type: Literal["token", "text"] = kwargs["oov_type"]
    return_type: Literal["first", "all"] = kwargs.get("return_type", "first")

    if oov_type == "token":
        if return_type == "all":
            oov_tokens = "".join(
                token
                for _, status, payload in processed
                for token in (payload or [])
                if status == "illicit_token"
            )
        elif return_type == "first":
            oov_tokens = next(
                (
                    token
                    for _, status, payload in processed
                    for token in (payload or [])
                    if status == "illicit_token"
                ),
                # TODO: smarter handling of no OOV cases across all branches
                "no oov!",
            )
        else:
            raise ValueError(f"Invalid {return_type}")
        formatted = []
        oov_tokens = set(oov_tokens)
        for char in oov_tokens:
            formatted.append((TOKEN_STYLES.oov, char))
            formatted.append(("", "\t"))
            formatted.append(("", _unicode_point(char)))
            formatted.append(("", "\t"))
            formatted.append(("", unicodedata.name(char)))
            formatted.append(("", "\n"))
        return FormattedText(formatted), state

    if oov_type == "text":
        if return_type == "all":
            oov_texts = tuple(
                text for text, status, _ in processed if status != "licit"
            )
            return FormattedText(
                zip(
                    oov_texts,
                    [TOKEN_STYLES.oov.value] * len(oov_texts),
                )
            ), state
        elif return_type == "first":
            oov_text = next(text for text, status, _ in processed if status != "licit")
            return FormattedText(
                [
                    (
                        TOKEN_STYLES.oov.value,
                        oov_text,
                    )
                ]
            ), state
        else:
            raise ValueError(f"Invalid {return_type=}")
        raise ValueError(f"Invalid {oov_type=}")


def _process_all_texts(state: ReplState, start: int = 0) -> tqdm:
    spec = get_active_spec(state)
    if spec is None:
        raise NoActiveSpecError

    processed = tqdm(
        (
            normalize(
                text.string,
                spec,
                state.registry,
            )
            for text in state.texts[start:]
        ),
        total=len(state.texts) - start,
    )
    return processed


def handle_save(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    path = kwargs["path"]
    try:
        Path(path).with_suffix(".json").write_text(
            json.dumps(
                asdict(state),
                indent=2,
                ensure_ascii=True,
            )
        )
    except Exception as e:
        raise StateSaveError(f"{path}: {e}") from e

    return f"Saved to {path}!", state


def handle_load(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    path = kwargs["path"]
    try:
        state_data = json.loads(Path(path).with_suffix(".json").read_bytes())
        state = structure(state_data, ReplState)
    except Exception as e:
        raise StateLoadError(f"{path}: {e}") from e
    return f"Loaded {path}!", state


def _unicode_point(char: str) -> str:
    code_point = ord(char)
    return f"U+{code_point:04X}"


def handle_nonascii(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    target: Literal["raw", "processed"] = kwargs.get("target", "processed")
    if target == "processed":
        processed = _process_all_texts(state)
        texts = [text for text, _, _ in processed]
    elif target == "raw":
        texts = get_structs(
            struct_type_name=Text.__name__,
            registry=state.registry,
        )
        texts = [text.string for text in texts]
    else:
        raise ValueError(f"Invalid {target=}")

    joined = "".join(texts)
    chars = set(joined)
    is_nonascii = lambda c: not c.isascii()
    nonascii = list(filter(is_nonascii, chars))
    rows = [(char, _unicode_point(char), unicodedata.name(char)) for char in nonascii]
    cols = ["Character", "Unicode point", "Name"]
    title = f"{target.upper()} non-ascii characters"
    return _rich_table(
        title=title,
        cols=cols,
        rows=rows,
    ), state


def handle_look_unicode(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    text = _retrieve_text(kwargs, state)
    target: Literal["raw", "processed"] = kwargs.get("target", "processed")
    if target == "processed":
        spec = get_active_spec(state)
        if spec is None:
            raise NoActiveSpecError
        text, _, _ = normalize(
            text=text,
            spec=spec,
            registry=state.registry,
        )

    rows = [(char, _unicode_point(char), unicodedata.name(char)) for char in text]
    cols = ["Character", "Unicode point", "Name"]
    title = f"{target.upper()} character unicode data"
    return _rich_table(
        title=title,
        cols=cols,
        rows=rows,
    ), state


def handle_search(
    kwargs: dict[str, str],
    state: ReplState,
) -> tuple[FormattedText | str, ReplState]:
    query = kwargs["query"]
    hits = []
    for text in state.texts:
        if query in text.string:
            hits.append(text.string)
        if len(hits) >= MAX_OUTPUT_STRINGS:
            break
    return "\n".join(hits), state


_verb_to_func = {
    "add": handle_add,
    "list": handle_list,
    "remove": handle_remove,
    "tokenize": handle_tokenize,
    "process": handle_process,
    "read": handle_read,
    "spec": handle_spec,
    "apply": handle_apply,
    "oov": handle_oov,
    "save": handle_save,
    "load": handle_load,
    "nonascii": handle_nonascii,
    "look": handle_look_unicode,
    "map": handle_map,
    "search": handle_search,
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
