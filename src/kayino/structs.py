"""
# structs.py
Helpers for building and editing structs.
"""

import functools
from collections import defaultdict
from typing import Literal

from attrs import evolve
from cattrs import structure

from kayino.acceptor import fsa
from kayino.models import NAME_TO_STRUCT, Pattern, RegistryType, Token, TokenGroup

# struct name -> list of hooks
type StructHookType = dict[str, list[callable]]

# struct name -> struct field -> single hook
type KeyedStructHookType = dict[str, dict[str, callable]]

# enumerate type for struct mutations
type MutationType = Literal["update", "append", "remove"]

# struct name -> struct field -> mutation type -> single hook
type MutationStructHookType = dict[str, dict[str, dict[MutationType, callable]]]

"""
## Struct validation hooks

Functions to call when inserting or updating a struct.
Each hooks takes the newly constructed struct, the name
of the field to validate, and the registry containing
all existing structs.
"""


class FsaCompilationError(Exception):
    pass


def _validate_pattern_compiles(
    struct: object,
    field: str,
    registry: RegistryType,
):
    try:
        fsa(getattr(struct, field), registry)
    except Exception as error:
        raise FsaCompilationError(
            f"Pattern string {getattr(struct, field)} could not be compiled "
            "to an FSA, original error: " + str(error)
        )


_struct_validation_hooks: StructHookType = defaultdict(list)
_struct_validation_hooks[Pattern.__name__].append(
    functools.partial(
        _validate_pattern_compiles,
        field="value",
    ),
)


def validate_stuct(
    struct: object,
    registry: RegistryType,
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


class MalformedStructArrayError(Exception):
    pass


def _array_ids_contiguous(
    struct_type_name: str,
    field: str,
    registry: RegistryType,
):
    id_array = [getattr(struct, field) for struct in registry[struct_type_name]]
    if id_array != list(range(len(id_array))):
        raise MalformedStructArrayError


def _array_ids_unique(
    struct_type_name: str,
    field: str,
    registry: RegistryType,
):
    id_array = [getattr(struct, field) for struct in registry[struct_type_name]]
    if len(id_array) != len(set(id_array)):
        raise MalformedStructArrayError


_registry_validation_hooks: StructHookType = defaultdict(list)

for string_struct in [Token, TokenGroup, Pattern]:
    unique_string_func = functools.partial(
        _array_ids_unique,
        field="string",
    )
    _registry_validation_hooks[string_struct.__name__].append(unique_string_func)

# No need to specify `_array_ids_unique` for Token.id as uniqueness
# is a proper subset of contiguity
_registry_validation_hooks[Token.__name__].append(
    functools.partial(_array_ids_contiguous, field="id")
)


def validate_structs_in_registry(
    struct_type_name: str,
    registry: RegistryType,
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


def _get_available_key(
    struct_type_name: str,
    registry: RegistryType,
    **_,
) -> int:
    return len(registry[struct_type_name])


_construction_hooks: KeyedStructHookType = defaultdict(dict)
_construction_hooks[Token.__name__]["id"] = _get_available_key


def construct_fields(struct_type_name: str, registry: RegistryType, **kwargs) -> dict:
    for field, func in _construction_hooks[struct_type_name].items():
        value = func(
            struct_type_name,
            registry,
            **kwargs,
        )
        kwargs[field] = value
    return kwargs


def build_struct(struct_type_name: str, registry: RegistryType, **kwargs) -> object:
    kwargs = construct_fields(struct_type_name, registry, **kwargs)
    struct_type = NAME_TO_STRUCT[struct_type_name]
    struct = structure(kwargs, struct_type)
    validate_stuct(struct, registry)
    return struct


"""
## Registry mutation hooks

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
    new_struct = evolve(struct, **{field: new_array})
    return new_struct


def _remove_item_from_array(
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
    new_struct = evolve(struct, **{field: new_array})
    return new_struct


# You'd better be ready for how default this dictionary is
_struct_mutation_hooks: MutationStructHookType = defaultdict(lambda: defaultdict(dict))

_struct_mutation_hooks[TokenGroup.__name__]["members"] = {
    "append": _add_item_to_array,
    "remove": _remove_item_from_array,
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
    registry: RegistryType,
) -> RegistryType:
    struct_array = registry[struct_type_name]
    reindexed = [evolve(struct, **{field: i}) for i, struct in enumerate(struct_array)]
    registry[struct_type_name] = reindexed
    return registry


_post_remove_hooks: StructHookType = defaultdict(list)
_post_remove_hooks[Token.__name__].append(
    functools.partial(
        _reindex_tokens,
        field="id",
    )
)


def register_struct(
    registry: RegistryType,
    struct: object,
) -> tuple[object, RegistryType]:
    """
    Append `struct` to the appropriate list in the registry
    and runs any registry-level validation.
    Assumes `struct` has already been validated by another
    function, e.g. `build_struct`.

    Returns the mutated registry.
    """
    registry[type(struct).__name__].append(struct)
    validate_structs_in_registry(type(struct).__name__, registry)
    return registry


def remove_struct(
    struct: object,
    registry: RegistryType,
) -> RegistryType:
    """
    Remove `struct` from the appropriate list in the registry,
    applies any struct removal hooks if any, and runs any
    registry-level validation.

    Returns the mutated registry.
    """
    struct_type_name = type(struct).__name__
    struct_array = registry[struct_type_name]
    filtered_struct_array = [
        curr_struct for curr_struct in struct_array if struct != curr_struct
    ]
    registry[struct_type_name] = filtered_struct_array
    for func in _post_remove_hooks[struct_type_name]:
        func(struct_type_name, registry)
    validate_structs_in_registry(struct_type_name, registry)
    return registry


"""
# Retrieval functions
"""


def fetch_struct(
    struct_type_name: str,
    field: str,
    value: object,
    registry: RegistryType,
) -> object:
    """
    Fetch a struct whose field matches the specified value (e.g.
    an id or other unique identifier). Raises a ValueError if multiple
    such structrs are found.
    """
    filtered = filter_structs(struct_type_name, field, value, registry)
    if len(filtered) > 1:
        raise ValueError(
            f"Expected only one struct with {field}={value} but got {filtered}"
        )
    if not filtered:
        raise KeyError(f"No struct with {field}={value}")

    return filtered[0]


def filter_structs(
    struct_type_name: str,
    field: str,
    value: object,
    registry: RegistryType,
) -> list[object]:
    return [
        struct
        for struct in registry[struct_type_name]
        if getattr(struct, field) == value
    ]
