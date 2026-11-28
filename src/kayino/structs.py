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
    ID_FIELDS,
    MACRO_STRUCT_TYPES,
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
    StructRenameError,
    Symbol,
    get_structs,
    resolve_macro_struct_type_name,
    resolve_struct_type_fields,
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

Each registry validation hook must take a struct type name and a
registry as input args.
"""


def _array_ids_unique(
    struct_type_name: str,
    field: str,
    registry: Registry,
):
    macro_struct_name = resolve_macro_struct_type_name(struct_type_name)
    id_array = [
        getattr(struct, field) for struct in get_structs(macro_struct_name, registry)
    ]
    if len(id_array) != len(set(id_array)):
        raise MalformedStructArrayError


_registry_validation_hooks: StructHookType = defaultdict(list)

# ID_FIELD attributes must always be unique
for struct_type_name in MACRO_STRUCT_TYPES:
    struct_fields = resolve_struct_type_fields(struct_type_name)
    for field in ID_FIELDS:
        if field in struct_fields:
            _registry_validation_hooks[struct_type_name].append(
                functools.partial(
                    _array_ids_unique,
                    field=field,
                )
            )
_registry_validation_hooks[Symbol.__name__].append(
    functools.partial(
        _array_ids_unique,
        field="string",
    )
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

_field_hooks: KeyedStructHookType = defaultdict(dict)


def construct_fields(struct_type_name: str, registry: Registry, **kwargs) -> dict:
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


def _add_pair_to_map(
    struct: object,
    field: str,
    data: object,
) -> object:
    """
    Mutate struct by appending a key:value pair
    at the specified field.
    """
    key, value = data
    existing_map = getattr(struct, field)
    new_map = existing_map | {key: value}
    return new_map


def _pop_pair_from_map(
    struct: object,
    field: str,
    data: object,
) -> object:
    """
    Mutate struct by removing a key:value pair
    at the specified field.
    """
    existing_map = getattr(struct, field)
    new_map = frozendict({k: v for k, v in existing_map.items() if k != data})
    return new_map


# You'd better be ready for how default this dictionary is
_struct_mutation_hooks: MutationStructHookType = defaultdict(lambda: defaultdict(dict))

_struct_mutation_hooks[NormalizationSpec.__name__]["steps"] = {
    "append": _add_item_to_array,
    "pop": _pop_item_from_array,
}
_struct_mutation_hooks[NormalizationSpec.__name__]["licit_patterns"] = {
    "append": _add_item_to_array,
    "pop": _pop_item_from_array,
}
_struct_mutation_hooks[MappingNorm.__name__]["data"] = {
    "append": _add_pair_to_map,
    "pop": _pop_pair_from_map,
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

    For now, renaming structs is not allowed, so throw an error
    if the caller attempts to mutate an ID field.
    """

    if field in ID_FIELDS:
        raise StructRenameError("Renaming structs not supported.")

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


def register_struct(
    struct: object,
    registry: Registry,
) -> Registry:
    """
    Append `struct` to the appropriate list in the registry
    and runs any registry-level validation.
    Assumes `struct` has already been validated before being
    passed.

    Returns the mutated registry.
    """
    struct_type_name = resolve_macro_struct_type_name(type(struct).__name__)
    curr_struct_array = get_structs(struct_type_name, registry)
    new_struct_array = curr_struct_array + (struct,)
    new_registry = evolve(registry, **{struct_type_name: new_struct_array})
    validate_structs_in_registry(struct_type_name, new_registry)
    return new_registry


def _check_depending_structs(
    struct: object,
    struct_type_name: str,
    id_field: str,
    foreign_type_name: str,
    foreign_field: str,
    foreign_field_type: Literal["reference", "reference_array"],
    registry: Registry,
):
    current_id = getattr(struct, id_field)
    foreign_structs = get_structs(
        struct_type_name=foreign_type_name,
        registry=registry,
    )
    if foreign_field_type == "reference":
        does_depend = lambda foreign: current_id == getattr(foreign, foreign_field)
    elif foreign_field_type == "reference_array":
        does_depend = lambda foreign: current_id in getattr(foreign, foreign_field)
    else:
        raise ValueError
    depends = tuple(filter(does_depend, foreign_structs))
    if depends:
        raise StructDependencyError(
            f"{depends} depend on existing struct {struct}, cannot remove."
        )


# Pre-remove hooks take struct_type_name and registry as args
# and raise an error if some constraint is not met

_pre_remove_hooks: StructHookType = defaultdict(list)
_pre_remove_hooks[NormStep.__name__].append(
    functools.partial(
        _check_depending_structs,
        foreign_type_name=NormalizationSpec.__name__,
        id_field="name",
        foreign_field="steps",
        foreign_field_type="reference_array",
    )
)

# Post-remove hooks take struct_type_name and registry as args
# and return a mutated registry

_post_remove_hooks: StructHookType = defaultdict(list)


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
    struct_type_name = resolve_macro_struct_type_name(type(struct).__name__)
    for func in _pre_remove_hooks[struct_type_name]:
        func(
            struct=struct,
            struct_type_name=struct_type_name,
            registry=registry,
        )

    struct_array = get_structs(struct_type_name, registry)
    filtered_struct_array = tuple(
        curr_struct for curr_struct in struct_array if struct != curr_struct
    )
    new_registry = evolve(registry, **{struct_type_name: filtered_struct_array})
    for func in _post_remove_hooks[struct_type_name]:
        new_registry = func(
            struct=struct,
            struct_type_name=struct_type_name,
            registry=new_registry,
        )
    validate_structs_in_registry(struct_type_name, new_registry)
    return new_registry


def put_struct(
    old_struct: object,
    new_struct: object,
    registry: Registry,
) -> Registry:
    if type(old_struct) is not type(new_struct):
        raise ValueError("Cannot update struct with struct of different type!")
    for field in ID_FIELDS:
        if not hasattr(old_struct, field):
            continue
        if getattr(old_struct, field) != getattr(new_struct, field):
            raise StructRenameError("Renaming structs not supported.")

    struct_type_name = resolve_macro_struct_type_name(type(old_struct).__name__)
    struct_array = get_structs(struct_type_name, registry)
    filtered_struct_array = tuple(
        curr_struct for curr_struct in struct_array if curr_struct != old_struct
    )
    new_struct_array = filtered_struct_array + (new_struct,)
    new_registry = evolve(registry, **{struct_type_name: new_struct_array})

    return new_registry


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


def init_registry() -> Registry:
    data = {}
    prebuilt = tuple(PREBUILT_NORMS.values())
    data[NormStep.__name__] = prebuilt
    registry = Registry(**data)
    return registry
