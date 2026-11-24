"""
# structs.py
Helpers for building and editing structs.
"""

import functools
from collections import defaultdict
from collections.abc import Callable
from typing import Literal

from attrs import evolve
from cattrs import structure
from frozendict import frozendict

from kayino.acceptor import fsa
from kayino.models import (
    NAME_TO_STRUCT,
    TYPE_TO_UNION,
    DuplicateStructError,
    FsaCompilationError,
    MalformedStructArrayError,
    MappingNorm,
    NormalizationSpec,
    NormStep,
    Pattern,
    Registry,
    StructDependencyError,
    StructNotFoundError,
    Text,
    Token,
    TokenGroup,
    resolve_struct_type_name,
)
from kayino.prebuilt import PREBUILT_NORMS

# struct name -> list of hooks
type StructHookType = dict[str, list[Callable]]

# struct name -> struct field -> single hook
type KeyedStructHookType = dict[str, dict[str, Callable]]

# enumerated type for registry mutations
type MutationType = Literal["update", "append", "pop"]

# struct name -> struct field -> mutation type -> single hook
type MutationStructHookType = dict[str, dict[str, dict[MutationType, Callable]]]

"""
## Struct validation hooks

Functions to call when inserting or updating a struct.
Each hooks takes the newly constructed struct, the name
of the field to validate, and the registry containing
all existing structs.
"""


def _validate_pattern_compiles(
    struct: object,
    field: str,
    registry: Registry,
):
    try:
        fsa(getattr(struct, field), registry)
    except Exception as error:
        raise FsaCompilationError(
            f"Pattern string {getattr(struct, field)} could not be compiled "
            "to an FSA, original error: " + str(error)
        ) from error


_struct_validation_hooks: StructHookType = defaultdict(list)
_struct_validation_hooks[Pattern.__name__].append(
    functools.partial(
        _validate_pattern_compiles,
        field="value",
    ),
)


def validate_struct(
    struct: object,
    registry: Registry,
):
    for validation_func in _struct_validation_hooks[type(struct).__name__]:
        validation_func(
            struct=struct,
            registry=registry,
        )


"""
## Registry validation hooks

Unlike struct validation hooks, registry validation hooks
scope over all structs of a given type in the registry,
and so take a struct_type_name argument rather than an instantiated
struct.
"""


def _array_ids_contiguous(
    struct_type_name: str,
    field: str,
    registry: Registry,
):
    id_array = [
        getattr(struct, field) for struct in get_structs(struct_type_name, registry)
    ]
    if id_array != list(range(len(id_array))):
        raise MalformedStructArrayError


def _array_ids_unique(
    struct_type_name: str,
    field: str,
    registry: Registry,
    all_struct_types_checked: list[str] | None = None,
):
    if all_struct_types_checked is None:
        id_array = [
            getattr(struct, field) for struct in get_structs(struct_type_name, registry)
        ]
    else:
        id_array = [
            getattr(struct, field)
            for checked_type in all_struct_types_checked
            for struct in get_structs(checked_type, registry)
        ]
    if len(id_array) != len(set(id_array)):
        raise MalformedStructArrayError


_registry_validation_hooks: StructHookType = defaultdict(list)

# Token, TokenGroup and Pattern structs all require
# unique "string" field
for string_struct in [Token, TokenGroup, Pattern]:
    unique_string_func = functools.partial(
        _array_ids_unique,
        field="string",
        all_struct_types_checked=(
            Token.__name__,
            TokenGroup.__name__,
            Pattern.__name__,
        ),
    )
    _registry_validation_hooks[string_struct.__name__].append(unique_string_func)

# No need to specify `_array_ids_unique` for Token.id as uniqueness
# is a proper subset of contiguity
_registry_validation_hooks[Token.__name__].append(
    functools.partial(_array_ids_contiguous, field="id")
)


def validate_structs_in_registry(
    struct_type_name: str,
    registry: Registry,
):
    for validation_func in _registry_validation_hooks[struct_type_name]:
        validation_func(
            struct_type_name=struct_type_name,
            registry=registry,
        )


"""
## Struct construction hooks

Struct construction hooks define logic for defining or transforming
the data for a given field for a struct.
Each hook takes as parameters a registry and a set of kwargs containing
existing field data for the struct being constructed.
"""


def _get_available_consecutive_key(
    struct_type_name: str,
    registry: Registry,
    **_,
) -> int:
    return len(get_structs(struct_type_name, registry))


def _get_available_nonconsecutive_key(
    struct_type_name: str,
    registry: Registry,
    **_,
) -> int:
    return (
        max(
            (struct.id for struct in get_structs(struct_type_name, registry)),
            default=-1,
        )
        + 1
    )


_field_hooks: KeyedStructHookType = defaultdict(dict)
_field_hooks[Token.__name__]["id"] = _get_available_consecutive_key
_field_hooks[Text.__name__]["id"] = _get_available_nonconsecutive_key


def _rename_field(
    old_field: str,
    new_field: str,
    **kwargs,
) -> dict:
    if old_field in kwargs:
        if new_field in kwargs:
            raise ValueError(
                f"Expected one of {old_field}, {new_field} in kwargs, got both."
            )
        kwargs[new_field] = kwargs.pop(old_field)
    return kwargs


def _parse_members(
    foreign_type_name: str,
    foreign_string_field: str,
    foreign_id_field: str,
    **kwargs,
) -> dict:
    if string_list := kwargs.get("string_list", None):
        pass
    if id_list := kwargs.get("id_list", None):
        pass


_construction_hooks: StructHookType = defaultdict(list)
_construction_hooks[MappingNorm.__name__].append(
    functools.partial(_rename_field, old_field="mapping", new_field="data")
)


def construct_fields(struct_type_name: str, registry: Registry, **kwargs) -> dict:
    for func in _construction_hooks[struct_type_name]:
        kwargs = func(**kwargs)
    for field, func in _field_hooks[struct_type_name].items():
        value = func(
            struct_type_name=struct_type_name,
            registry=registry,
            **kwargs,
        )
        kwargs[field] = value
    return kwargs


def build_struct(
    struct_type_name: str,
    registry: Registry,
    **kwargs,
) -> tuple[object, Registry]:
    kwargs = construct_fields(struct_type_name, registry, **kwargs)
    struct_type = NAME_TO_STRUCT[struct_type_name]
    struct = structure(kwargs, struct_type)
    validate_struct(struct, registry)
    registry = register_struct(struct, registry)
    return struct, registry


"""
## Struct mutation hooks

Hooks for updating a given field on a struct.
Each field may define multiple types of mutations, hence the
use of the `NamedStructHook` type.
"""


def _add_item_to_array(
    struct: object,
    field: str,
    data: object,
) -> object:
    """
    Mutate struct by appending an item to the tuple
    at the specified field.
    """
    existing_array = getattr(struct, field)
    new_array = existing_array + (data,)
    return new_array


def _pop_item_from_array(
    struct: object,
    field: str,
    data: object,
) -> object:
    """
    Mutate struct by removing item from the tuple
    at the specified field.
    """
    existing_array = getattr(struct, field)
    new_array = tuple(curr_item for curr_item in existing_array if curr_item != data)
    return new_array


# You'd better be ready for how default this dictionary is
_struct_mutation_hooks: MutationStructHookType = defaultdict(lambda: defaultdict(dict))

_struct_mutation_hooks[TokenGroup.__name__]["members"] = {
    "append": _add_item_to_array,
    "pop": _pop_item_from_array,
}
_struct_mutation_hooks[NormalizationSpec.__name__]["steps"] = {
    "append": _add_item_to_array,
    "pop": _pop_item_from_array,
}
_struct_mutation_hooks[NormalizationSpec.__name__]["licit_patterns"] = {
    "append": _add_item_to_array,
    "pop": _pop_item_from_array,
}


def mutate_struct(
    struct: object,
    field: str,
    data: object,
    mutation_type: MutationType,
) -> object:
    """
    Mutate a given field value for a struct and return the
    updated struct.

    If mutation_type is "update", simply replace the current
    field data with the passed `data`. Otherwise, call a registered
    hook for the mutation operation.
    """

    if mutation_type == "update":
        value = data
    else:
        func = _struct_mutation_hooks[type(struct).__name__][field][mutation_type]
        value = func(
            struct=struct,
            field=field,
            data=data,
        )

    return evolve(struct, **{field: value})


"""
## Registry mutation helpers

Inserts, updates or removes a struct from the registry
and performs any necessary validation.
"""


def _reindex_tokens(
    struct_type_name: str,
    field: str,
    registry: Registry,
    **_,
) -> Registry:
    struct_array = get_structs(struct_type_name, registry)
    reindexed = tuple(
        evolve(struct, **{field: i}) for i, struct in enumerate(struct_array)
    )
    new_registry = frozendict(**registry | {struct_type_name: reindexed})
    return new_registry


def _check_depending_structs(
    struct: object,
    struct_type_name: str,
    foreign_type_name: str,
    id_field: str,
    foreign_field: str,
    registry: Registry,
):
    current_id = getattr(struct, id_field)
    foreign_structs = get_structs(
        struct_type_name=foreign_type_name,
        registry=registry,
    )
    does_depend = lambda foreign: current_id in getattr(foreign, foreign_field)
    depends = tuple(filter(does_depend, foreign_structs))
    if depends:
        raise StructDependencyError(
            f"{depends} depend on existing struct {struct}, cannot remove."
        )


_pre_remove_hooks: StructHookType = defaultdict(list)
_pre_remove_hooks[Token.__name__].append(1)

_post_remove_hooks: StructHookType = defaultdict(list)
_post_remove_hooks[Token.__name__].append(
    functools.partial(
        _reindex_tokens,
        field="id",
    )
)


def register_struct(
    struct: object,
    registry: Registry,
) -> Registry:
    """
    Append `struct` to the appropriate list in the registry
    and runs any registry-level validation.
    Assumes `struct` has already been validated by another
    function, e.g. `build_struct`.

    Returns the mutated registry.
    """
    struct_type_name = resolve_struct_type_name(type(struct).__name__)
    curr_struct_array = get_structs(struct_type_name, registry)
    new_struct_array = curr_struct_array + (struct,)
    new_registry = frozendict(registry | {struct_type_name: new_struct_array})
    validate_structs_in_registry(struct_type_name, new_registry)
    return new_registry


def remove_struct(
    struct: object,
    registry: Registry,
) -> Registry:
    """
    Remove `struct` from the appropriate list in the registry,
    applies any struct removal hooks if any, and runs any
    registry-level validation.

    Returns the mutated registry.
    """
    struct_type_name = resolve_struct_type_name(type(struct).__name__)
    struct_array = get_structs(struct_type_name, registry)
    filtered_struct_array = tuple(
        curr_struct for curr_struct in struct_array if struct != curr_struct
    )
    new_registry = frozendict(**registry | {struct_type_name: filtered_struct_array})
    for func in _post_remove_hooks[struct_type_name]:
        new_registry = func(
            struct=struct,
            struct_type_name=struct_type_name,
            registry=new_registry,
        )
    validate_structs_in_registry(struct_type_name, new_registry)
    return new_registry


def update_struct(
    old_struct: object,
    new_struct: object,
    registry: Registry,
) -> Registry:
    if type(old_struct) is not type(new_struct):
        raise ValueError("Cannot update struct with struct of different type!")
    registry = remove_struct(
        struct=old_struct,
        registry=registry,
    )
    registry = register_struct(
        struct=new_struct,
        registry=registry,
    )
    return registry


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
    an id or other unique identifier). Raises a ValueError if multiple
    such structrs are found.
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
## Registry helpers
"""


def init_registry() -> Registry:
    registry = dict.fromkeys(
        set(NAME_TO_STRUCT.keys()) - set(TYPE_TO_UNION.keys()),
        (),
    )
    prebuilt = tuple(PREBUILT_NORMS.values())
    registry[NormStep.__name__] = prebuilt
    registry = frozendict(registry)
    return registry


def get_structs(
    struct_type_name: str,
    registry: Registry,
) -> tuple[object, ...]:
    if union_type := TYPE_TO_UNION.get(struct_type_name, None):
        union_struct_array = registry[union_type]
        filtered = tuple(
            filter(
                (lambda struct: type(struct).__name__ == struct_type_name),
                union_struct_array,
            )
        )
        return filtered
    return registry[struct_type_name]
