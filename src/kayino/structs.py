"""
# structs.py
Helpers for building, editing and querying structs.

The registry maps table names (see `kayino.models.TABLES`) to tuples of
row structs. Functions that take a `struct_type_name` accept a table name,
a row class name (e.g. "Token") or a union alias name (e.g. "NormStep").

Every change goes through `_commit`, which runs `check()` on the new
registry and raises `RegistryCheckError` if anything is violated. Because
the registry is immutable, a failed change leaves the caller's registry
untouched.
"""

from collections.abc import Callable

from attrs import evolve
from frozendict import frozendict

from kayino.acceptor import fsa, surface, tokenize_str
from kayino.models import (
    CONVERTER,
    TABLE_BY_NAME,
    TABLES,
    DuplicateStructError,
    KeyChangeError,
    Pattern,
    PrebuiltNorm,
    Registry,
    RegistryCheckError,
    RenameError,
    StructNotFoundError,
    Text,
    Token,
    TokenGroup,
    row_type_for,
    table_for,
)
from kayino.prebuilt import PREBUILT_NORM_FUNCS, PREBUILT_NORMS
from kayino.schema import Violation, check_registry

"""
## Checking

`check()` runs the generic checks derived from `TABLES` (keys, refs, enums),
then, only if those pass, the Kayino-specific `CUSTOM_CHECKS`.
"""


def _patterns_compile(registry: Registry) -> list[Violation]:
    violations = []
    for pattern in get_structs(Pattern.__name__, registry):
        try:
            fsa(pattern.value, registry)
        except Exception as error:
            violations.append(
                Violation(
                    table="symbols",
                    key=(pattern.name,),
                    column="value",
                    kind="custom",
                    note=f"pattern {pattern.value!r} does not compile: {error}",
                )
            )
    return violations


def _prebuilt_norms_exist(registry: Registry) -> list[Violation]:
    return [
        Violation(
            table="norm_steps",
            key=(norm.name,),
            column="name",
            kind="custom",
            note="no prebuilt function with this name",
        )
        for norm in get_structs(PrebuiltNorm.__name__, registry)
        if norm.name not in PREBUILT_NORM_FUNCS
    ]


CUSTOM_CHECKS: list[Callable[[Registry], list[Violation]]] = [
    _patterns_compile,
    _prebuilt_norms_exist,
]


def check(registry: Registry) -> list[Violation]:
    violations = check_registry(registry, TABLES)
    if violations:
        return violations
    return [
        violation for custom_check in CUSTOM_CHECKS for violation in custom_check(registry)
    ]


def _commit(registry: Registry) -> Registry:
    if violations := check(registry):
        raise RegistryCheckError(violations)
    return registry


"""
## Building and changing structs
"""


def next_id(registry: Registry, table_name: str) -> int:
    """An unused integer id for a table keyed on `id` (e.g. texts)."""
    return max((row.id for row in registry[table_name]), default=-1) + 1


def build_struct(
    struct_type_name: str,
    registry: Registry,
    **kwargs,
) -> tuple[object, Registry]:
    """
    Structure `kwargs` into a struct of the given type and insert it.
    Returns the struct and the new registry.
    """
    struct_type = row_type_for(struct_type_name)
    if struct_type is Text and "id" not in kwargs:
        kwargs["id"] = next_id(registry, "texts")
    struct = CONVERTER.structure(kwargs, struct_type)
    return struct, insert_struct(struct, registry)


def insert_struct(
    struct: object,
    registry: Registry,
) -> Registry:
    """Append `struct` to its table."""
    table_name = table_for(type(struct).__name__).name
    return _commit(frozendict(registry | {table_name: registry[table_name] + (struct,)}))


def _owned_rows(
    struct: object,
    registry: Registry,
) -> list[object]:
    """
    Rows that reference `struct` through a `ref(..., on_delete="cascade")`,
    and the rows they own in turn.
    """
    parent_table = table_for(type(struct).__name__).name
    owned = []
    for table in TABLES:
        for column, ref_ in table.refs().items():
            if ref_.table != parent_table or ref_.on_delete != "cascade":
                continue
            if ref_.kind is not None and getattr(struct, "kind", None) != ref_.kind:
                continue
            parent_value = getattr(struct, ref_.column)
            for child in registry[table.name]:
                if getattr(child, column, None) == parent_value:
                    owned.append(child)
                    owned.extend(_owned_rows(child, registry))
    return owned


def remove_struct(
    struct: object,
    registry: Registry,
) -> Registry:
    """
    Remove `struct` and the rows it owns (cascade). Fails if any remaining
    row still references it (restrict).
    """
    tables = dict(registry)
    for row in [struct, *_owned_rows(struct, registry)]:
        table_name = table_for(type(row).__name__).name
        tables[table_name] = tuple(r for r in tables[table_name] if r != row)
    return _commit(frozendict(tables))


def replace_struct(
    old_struct: object,
    new_struct: object,
    registry: Registry,
) -> Registry:
    """
    Replace `old_struct` with `new_struct` in place, keeping its position
    and its owned rows. The key may not change: use `rename_struct`.
    """
    if type(old_struct) is not type(new_struct):
        raise ValueError("Cannot update struct with struct of different type!")
    table = table_for(type(old_struct).__name__)
    if table.key_of(old_struct) != table.key_of(new_struct):
        raise KeyChangeError(
            f"Cannot change the key of {old_struct} with replace; use rename."
        )
    rows = registry[table.name]
    if old_struct not in rows:
        raise StructNotFoundError(f"{old_struct} is not in the registry")
    index = rows.index(old_struct)
    new_rows = rows[:index] + (new_struct,) + rows[index + 1 :]
    return _commit(frozendict(registry | {table.name: new_rows}))


"""
## Renaming

Only tables with a single-column key that some `ref` points at can be
renamed: currently symbols, norm_steps and specs.
"""

RENAMEABLE_TABLES = frozenset(
    table.name
    for table in TABLES
    if len(table.key) == 1
    and any(
        ref_.table == table.name and ref_.column == table.key[0]
        for other in TABLES
        for ref_ in other.refs().values()
    )
)


def _is_symbol(token: object) -> bool:
    return isinstance(token, (Token, TokenGroup, Pattern))


def _signature(tokens) -> tuple[tuple[str, str], ...]:
    """How a pattern string tokenizes, ignoring the empty splits."""
    return tuple(
        (type(token).__name__, surface(token)) for token in tokens if surface(token)
    )


def rename_struct(
    struct_type_name: str,
    old_name: str,
    new_name: str,
    registry: Registry,
) -> Registry:
    """
    Change a row's key from `old_name` to `new_name`, updating every column
    that references it. When renaming a symbol, pattern strings mentioning it
    are rewritten too, and the rename is refused if it would change how any
    pattern tokenizes (longest match can make a new name swallow
    neighbouring text).
    """
    table = table_for(struct_type_name)
    if table.name not in RENAMEABLE_TABLES:
        raise RenameError(f"{struct_type_name} cannot be renamed")
    (key_column,) = table.key
    row = fetch_struct(struct_type_name, key_column, old_name, registry)

    tables = {name: list(rows) for name, rows in registry.items()}
    target_rows = tables[table.name]
    target_rows[target_rows.index(row)] = evolve(row, **{key_column: new_name})

    for other in TABLES:
        for column, ref_ in other.refs().items():
            if ref_.table == table.name and ref_.column == key_column:
                tables[other.name] = [
                    evolve(r, **{column: new_name})
                    if getattr(r, column, None) == old_name
                    else r
                    for r in tables[other.name]
                ]

    # index in `symbols` -> expected tokenization after the rename
    expected_signatures: dict[int, tuple] = {}
    if table.name == "symbols":
        symbols = tables["symbols"]
        for index, symbol in enumerate(symbols):
            if not isinstance(symbol, Pattern):
                continue
            original = registry["symbols"][index]
            tokens = tokenize_str(original.value, registry)
            if "".join(surface(token) for token in tokens) != original.value:
                raise RenameError(
                    f"Cannot rewrite pattern {original.name}: its value does not "
                    "round-trip through the tokenizer."
                )
            rewritten = [
                new_name if _is_symbol(token) and token.name == old_name else surface(token)
                for token in tokens
            ]
            expected_signatures[index] = tuple(
                (kind, new_name if kind != "AnonToken" and text == old_name else text)
                for kind, text in _signature(tokens)
            )
            symbols[index] = evolve(symbol, value="".join(rewritten))

    new_registry = frozendict({name: tuple(rows) for name, rows in tables.items()})

    shifted = [
        new_registry["symbols"][index].name
        for index, expected in expected_signatures.items()
        if _signature(tokenize_str(new_registry["symbols"][index].value, new_registry))
        != expected
    ]
    if shifted:
        raise RenameError(
            f"Renaming {old_name!r} to {new_name!r} would change how these "
            f"patterns tokenize: {', '.join(shifted)}"
        )
    return _commit(new_registry)


"""
# Retrieval functions
"""


def fetch_struct(
    struct_type_name: str,
    field: str,
    value: object,
    registry: Registry,
) -> object:
    """
    Fetch a struct whose field matches the specified value (e.g.
    an id or other unique identifier). Raises a DuplicateStructError if
    multiple such structs are found.
    """
    filtered = filter_structs(struct_type_name, field, value, registry)
    if len(filtered) > 1:
        raise DuplicateStructError(
            f"Expected only one struct with {field}={value} but got {filtered}"
        )
    if not filtered:
        raise StructNotFoundError(f"No struct with {field}={value}")

    return filtered[0]


def filter_structs(
    struct_type_name: str,
    field: str,
    value: object,
    registry: Registry,
) -> list[object]:
    return [
        struct
        for struct in get_structs(struct_type_name, registry)
        if getattr(struct, field) == value
    ]


"""
## Nested views

Child rows (group members, mapping pairs, spec steps and patterns) are
stored flat. These helpers are the only place the nested shape exists.
"""


def _ordered_children(
    table_name: str,
    parent_field: str,
    parent_value: str,
    registry: Registry,
) -> list[object]:
    table = TABLE_BY_NAME[table_name]
    rows = [row for row in registry[table_name] if getattr(row, parent_field) == parent_value]
    if table.position:
        rows.sort(key=lambda row: getattr(row, table.position))
    return rows


def group_members(registry: Registry, group: str) -> tuple[str, ...]:
    """Token names in a group, in order."""
    return tuple(
        row.token for row in _ordered_children("group_members", "group", group, registry)
    )


def mapping(registry: Registry, norm: str) -> dict[str, str]:
    """The intab -> outtab pairs of a mapping norm."""
    return {
        row.intab: row.outtab
        for row in _ordered_children("mapping_rows", "norm", norm, registry)
    }


def spec_steps(registry: Registry, spec: str) -> tuple[str, ...]:
    """Norm step names of a spec, in order."""
    return tuple(
        row.step for row in _ordered_children("spec_steps", "spec", spec, registry)
    )


def spec_patterns(registry: Registry, spec: str) -> tuple[str, ...]:
    """Licit pattern names of a spec."""
    return tuple(
        row.pattern for row in _ordered_children("spec_patterns", "spec", spec, registry)
    )


def next_position(
    registry: Registry,
    table_name: str,
    parent_field: str,
    parent_value: str,
) -> int:
    """Position for a row appended after a parent's existing children."""
    table = TABLE_BY_NAME[table_name]
    return (
        max(
            (
                getattr(row, table.position)
                for row in registry[table_name]
                if getattr(row, parent_field) == parent_value
            ),
            default=-1,
        )
        + 1
    )


"""
## Registry helpers
"""


def init_registry() -> Registry:
    registry = dict.fromkeys(TABLE_BY_NAME, ())
    registry["norm_steps"] = tuple(PREBUILT_NORMS.values())
    return frozendict(registry)


def get_structs(
    struct_type_name: str,
    registry: Registry,
) -> tuple[object, ...]:
    """
    Rows for a table name or union alias name, or the rows of one class
    for a row class name (e.g. only the Tokens in `symbols`).
    """
    table = table_for(struct_type_name)
    rows = registry[table.name]
    if struct_type_name in (table.name, table.type_name):
        return rows
    return tuple(row for row in rows if type(row).__name__ == struct_type_name)
