"""
# schema.py

Primitives for declaring Kayino's tables on the attrs classes themselves.

Each struct is one table row. Column-level constraints (foreign-key-like
references) are declared in attrs field metadata via `ref()`. Table-level
facts (name, row type, key, position column) are declared with `Table`.
Everything else (columns, types, enums, the on-disk schema) is derived from
these declarations.

This module has no knowledge of the concrete Kayino structs. See
`kayino.models.TABLES` for the actual table list.
"""

import functools
import typing
from collections.abc import Mapping, Sequence
from typing import Literal, TypeAliasType, get_args, get_origin

import attrs
from attrs import field, frozen
from cattrs import Converter
from cattrs.strategies import configure_tagged_union

"""
## Column-level declarations
"""

OnDelete = Literal["restrict", "cascade"]

REF_METADATA_KEY = "ref"


@frozen
class Ref:
    """
    A reference from one column to a column of another table.

    Attributes:
      table:     Name of the referenced table.
      column:    Name of the referenced column (should be the table's key).
      kind:      If set, the referenced row's `kind` must equal this value
                 (e.g. a group member must reference a symbol of kind "token").
      on_delete: "restrict" forbids deleting a referenced row; "cascade"
                 deletes the referencing rows along with it.
    """

    table: str
    column: str = "name"
    kind: str | None = None
    on_delete: OnDelete = "restrict"


def ref(
    table: str,
    column: str = "name",
    *,
    kind: str | None = None,
    on_delete: OnDelete = "restrict",
    **kwargs,
):
    """
    Declare an attrs field that references `table.column`.
    Extra kwargs are passed through to `attrs.field`.
    """
    metadata = dict(kwargs.pop("metadata", {}))
    metadata[REF_METADATA_KEY] = Ref(
        table=table,
        column=column,
        kind=kind,
        on_delete=on_delete,
    )
    return field(metadata=metadata, **kwargs)


"""
## Table-level declarations
"""


@frozen
class Table:
    """
    A table of structs.

    Attributes:
      name:     Table name, also used as the registry key and the CSV file stem.
      row_type: An attrs class, or a PEP 695 type alias over a union of attrs
                classes. Union members are distinguished by their `kind` field.
      key:      Columns whose values together identify a row uniquely.
      # markjos: why multiple columns?
      # where do we not have a single unique id col?
      # claude: the four child tables: group_members (group, position),
      # mapping_rows (norm, intab), spec_steps (spec, position) and
      # spec_patterns (spec, pattern). A synthetic id column would need
      # allocating and renumbering again (the plumbing we're deleting), and
      # it'd be a column people must invent by hand when adding a CSV row.
      # More importantly, these keys ARE constraints: (norm, intab) means
      # "one outtab per intab per mapping", (group, position) means "one
      # member per slot". With an id you'd still need those uniqueness
      # checks, so you'd end up with both. Table Schema and SQL support
      # composite keys natively.
      position: Optional integer column giving the row's order within its
                parent. Renumbered 0..n on dump.
    """

    name: str
    row_type: object
    key: tuple[str, ...]
    position: str | None = None

    @property
    def is_union(self) -> bool:
        return isinstance(self.row_type, TypeAliasType)

    @property
    def type_name(self) -> str:
        return self.row_type.__name__

    @property
    def row_classes(self) -> tuple[type, ...]:
        if self.is_union:
            return get_args(self.row_type.__value__)
        return (self.row_type,)

    def columns(self) -> tuple[str, ...]:
        """
        Column names in declaration order. For a union table, this is every
        field of every member, in order of first appearance.
        """
        seen: dict[str, None] = {}
        for cls in self.row_classes:
            for attribute in attrs.fields(cls):
                seen.setdefault(attribute.name, None)
        return tuple(seen)

    def refs(self) -> dict[str, Ref]:
        """Map each referencing column to its `Ref`."""
        found: dict[str, Ref] = {}
        for cls in self.row_classes:
            for attribute in attrs.fields(cls):
                if ref_ := attribute.metadata.get(REF_METADATA_KEY):
                    found[attribute.name] = ref_
        return found

    def key_of(self, row: object) -> tuple:
        """The values of `row`'s key columns."""
        return tuple(getattr(row, column) for column in self.key)

    def kinds(self) -> tuple[str, ...]:
        """The `kind` values of this table's row classes (empty if none)."""
        return tuple(
            kind_field.default
            for cls in self.row_classes
            if (kind_field := getattr(attrs.fields(cls), "kind", None)) is not None
        )


"""
## Converter
"""


def kind_tag(cls: type) -> str:
    return attrs.fields(cls).kind.default


def make_converter(tables: Sequence[Table]) -> Converter:
    """
    A cattrs converter that structures/unstructures each union table as a
    union tagged on the `kind` field.
    """
    converter = Converter()
    for table in tables:
        if table.is_union:
            configure_tagged_union(
                table.row_type.__value__,
                converter,
                tag_generator=kind_tag,
                tag_name="kind",
            )
    return converter


"""
## Sanity checks
"""


def check_tables(tables: Sequence[Table]) -> list[str]:
    """
    Check that a set of table declarations is internally consistent.
    Returns a list of human-readable problems (empty if none).
    """
    problems: list[str] = []
    by_name = {}
    for table in tables:
        if table.name in by_name:
            problems.append(f"duplicate table name {table.name!r}")
        by_name[table.name] = table

    seen_classes: dict[type, str] = {}
    for table in tables:
        for cls in table.row_classes:
            if cls in seen_classes:
                problems.append(
                    f"{cls.__name__} appears in both {seen_classes[cls]!r} "
                    f"and {table.name!r}"
                )
            seen_classes[cls] = table.name
            field_names = {a.name for a in attrs.fields(cls)}
            for column in table.key:
                if column not in field_names:
                    problems.append(
                        f"{table.name}: key column {column!r} missing from "
                        f"{cls.__name__}"
                    )
            if table.position and table.position not in field_names:
                problems.append(
                    f"{table.name}: position column {table.position!r} missing "
                    f"from {cls.__name__}"
                )
            if table.is_union and "kind" not in field_names:
                problems.append(
                    f"{table.name}: union member {cls.__name__} has no `kind` field"
                )

        if table.is_union and len(set(table.kinds())) != len(table.row_classes):
            problems.append(f"{table.name}: union members must have distinct kinds")

        for column, ref_ in table.refs().items():
            target = by_name.get(ref_.table)
            if target is None:
                problems.append(
                    f"{table.name}.{column}: references unknown table {ref_.table!r}"
                )
                continue
            if ref_.column not in target.columns():
                problems.append(
                    f"{table.name}.{column}: references unknown column "
                    f"{ref_.table}.{ref_.column}"
                )
            if ref_.kind is not None and ref_.kind not in target.kinds():
                problems.append(
                    f"{table.name}.{column}: kind {ref_.kind!r} is not a kind of "
                    f"{ref_.table} (has {target.kinds()})"
                )
    return problems


"""
## Registry checking

A generic check of a registry (table name -> tuple of rows) against the
declarations: keys are unique, refs resolve (with the right `kind`), and
`Literal` fields hold allowed values. Checks that need Kayino-specific
knowledge (e.g. patterns compile) live in `kayino.structs.CUSTOM_CHECKS`.
"""

# "file": problems only raw files can have (bad integer, missing CSV, ...),
# reported by Frictionless at the file boundary (see kayino.store).
ViolationKind = Literal["key", "ref", "enum", "custom", "file"]


@frozen
class Violation:
    """
    One way a registry breaks its declared constraints.

    Attributes:
      table:  Table containing the offending row.
      key:    The offending row's key values.
      column: The offending column, or None for a whole-row problem.
      kind:   Which kind of constraint was broken.
      note:   Human-readable explanation.
    """

    table: str
    key: tuple
    column: str | None
    kind: ViolationKind
    note: str

    def __str__(self) -> str:
        key = f" ({', '.join(repr(value) for value in self.key)})" if self.key else ""
        column = f".{self.column}" if self.column else ""
        return f"{self.table}{key}{column}: {self.note}"


@functools.cache
def type_hints(cls: type) -> dict[str, object]:
    """Resolved field annotations (works with `from __future__ import annotations`)."""
    return typing.get_type_hints(cls)


@functools.cache
def literal_fields(cls: type) -> dict[str, tuple]:
    """Fields of an attrs class annotated with `Literal[...]`, and their values."""
    hints = type_hints(cls)
    return {
        name: get_args(hint)
        for name, hint in hints.items()
        if get_origin(hint) is Literal
    }


def check_registry(
    registry: Mapping[str, Sequence[object]],
    tables: Sequence[Table],
) -> list[Violation]:
    violations: list[Violation] = []
    by_name = {table.name: table for table in tables}

    for table in tables:
        rows = registry.get(table.name, ())
        seen_keys: set[tuple] = set()
        for row in rows:
            key = table.key_of(row)
            if key in seen_keys:
                violations.append(
                    Violation(
                        table=table.name,
                        key=key,
                        column=None,
                        kind="key",
                        note=f"duplicate key ({', '.join(table.key)})",
                    )
                )
            seen_keys.add(key)
            for column, allowed in literal_fields(type(row)).items():
                value = getattr(row, column)
                if value not in allowed:
                    violations.append(
                        Violation(
                            table=table.name,
                            key=key,
                            column=column,
                            kind="enum",
                            note=f"{value!r} is not one of {allowed}",
                        )
                    )

    # (table, column) -> {value: row}, built lazily per referenced column
    indexes: dict[tuple[str, str], dict[object, object]] = {}
    for table in tables:
        for column, ref_ in table.refs().items():
            index_key = (ref_.table, ref_.column)
            if index_key not in indexes:
                indexes[index_key] = {
                    getattr(target, ref_.column): target
                    for target in registry.get(ref_.table, ())
                }
            index = indexes[index_key]
            for row in registry.get(table.name, ()):
                if not hasattr(row, column):
                    # union member without this column
                    continue
                value = getattr(row, column)
                target = index.get(value)
                if target is None:
                    note = f"{value!r} not found in {ref_.table}.{ref_.column}"
                elif ref_.kind is not None and getattr(target, "kind", None) != ref_.kind:
                    note = (
                        f"{value!r} is a {getattr(target, 'kind', None)}, "
                        f"not a {ref_.kind}, in {ref_.table}"
                    )
                else:
                    continue
                violations.append(
                    Violation(
                        table=table.name,
                        key=by_name[table.name].key_of(row),
                        column=column,
                        kind="ref",
                        note=note,
                    )
                )
    return violations
