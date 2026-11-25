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
from collections.abc import Sequence
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
## Violations

What a refused change or an invalid project folder reports.
"""

ViolationKind = Literal["key", "ref", "enum", "required", "type", "custom", "file"]


@frozen
class Violation:
    """
    One way the data breaks its declared constraints.

    Attributes:
      table:  Table containing the offending row.
      key:    The offending row's key values (empty when unknown).
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


"""
## Column types

Derived from the resolved annotations of a table's row classes.
"""


@functools.cache
def type_hints(cls: type) -> dict[str, object]:
    """Resolved field annotations (works with `from __future__ import annotations`)."""
    return typing.get_type_hints(cls)


def _hints(table: Table, column: str) -> list:
    return [h for cls in table.row_classes if (h := type_hints(cls).get(column)) is not None]


def is_integer_column(table: Table, column: str) -> bool:
    """`int` and `int | None` columns."""
    return any(hint is int or int in get_args(hint) for hint in _hints(table, column))


def column_enum(table: Table, column: str) -> tuple:
    """Allowed values of a `Literal[...]` column (merged across union members)."""
    values: dict = {}
    for hint in _hints(table, column):
        if get_origin(hint) is Literal:
            values.update(dict.fromkeys(get_args(hint)))
    return tuple(values)


"""
## SQLite schema

Each table becomes a STRICT SQLite table. Keys become PRIMARY KEYs, enums
named CHECKs, refs deferred FOREIGN KEYs with ON UPDATE CASCADE (renames
propagate) and ON DELETE CASCADE or NO ACTION (restrict, checked at the end
of the change). A ref restricted to a `kind` adds a hidden constant column
`<column>__kind`, so the foreign key is (value, kind) -> (name, kind).
"""

KIND_SUFFIX = "__kind"


def _q(name: str) -> str:
    return f'"{name}"'


def columns_sql(columns) -> str:
    return ", ".join(map(_q, columns))


def ddl(tables: Sequence[Table]) -> str:
    kinded_targets = {
        (ref_.table, ref_.column)
        for table in tables
        for ref_ in table.refs().values()
        if ref_.kind
    }
    statements = []
    for table in tables:
        integer_key = len(table.key) == 1 and is_integer_column(table, table.key[0])
        lines = []
        for column in table.columns():
            if integer_key and column == table.key[0]:
                lines.append(f"{_q(column)} INTEGER PRIMARY KEY")
                continue
            integer = is_integer_column(table, column)
            line = f"{_q(column)} {'INTEGER' if integer else 'TEXT'} NOT NULL"
            if enum := column_enum(table, column):
                allowed = ", ".join(f"'{value}'" for value in enum)
                line += f" CONSTRAINT {_q('enum_' + column)} CHECK ({_q(column)} IN ({allowed}))"
            elif column in table.key and not integer:
                line += f" CONSTRAINT {_q('required_' + column)} CHECK ({_q(column)} <> '')"
            lines.append(line)
        for column, ref_ in table.refs().items():
            if ref_.kind:
                hidden = _q(column + KIND_SUFFIX)
                lines.append(
                    f"{hidden} TEXT NOT NULL DEFAULT '{ref_.kind}' "
                    f"CHECK ({hidden} = '{ref_.kind}')"
                )
        if not integer_key:
            lines.append(f"PRIMARY KEY ({columns_sql(table.key)})")
        for target_table, target_column in sorted(kinded_targets):
            if target_table == table.name:
                lines.append(f"UNIQUE ({columns_sql([target_column, 'kind'])})")
        for column, ref_ in table.refs().items():
            child = [column, column + KIND_SUFFIX] if ref_.kind else [column]
            parent = [ref_.column, "kind"] if ref_.kind else [ref_.column]
            on_delete = "CASCADE" if ref_.on_delete == "cascade" else "NO ACTION"
            lines.append(
                f"FOREIGN KEY ({columns_sql(child)}) "
                f"REFERENCES {_q(ref_.table)} ({columns_sql(parent)}) "
                f"ON UPDATE CASCADE ON DELETE {on_delete} DEFERRABLE INITIALLY DEFERRED"
            )
        statements.append(
            f"CREATE TABLE {_q(table.name)} (\n  " + ",\n  ".join(lines) + "\n) STRICT;"
        )
    return "\n\n".join(statements)
