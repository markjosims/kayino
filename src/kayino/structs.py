"""
# structs.py

The registry is an in-memory SQLite database whose schema is generated from
`kayino.models.TABLES` (see `kayino.schema.ddl`). SQLite enforces keys,
references (including their `kind`), enums, cascade and restrict, and
propagates renames. This module adds what SQLite cannot know about: the
custom checks (patterns compile, prebuilt functions exist), the rewriting of
pattern strings on rename, and reading rows back as attrs structs.

Functions that take a `struct_type_name` accept a table name, a row class
name (e.g. "Token") or a union alias name (e.g. "NormStep").

Every change runs inside `with change(db):`. On any violation the change is
rolled back and `RegistryCheckError` is raised, so the database is left as it
was.
"""

import sqlite3
from contextlib import contextmanager

from kayino.acceptor import fsa, surface, tokenize_str
from kayino.models import (
    CONVERTER,
    TABLE_BY_NAME,
    TABLES,
    DuplicateStructError,
    Pattern,
    PrebuiltNorm,
    Registry,
    RegistryCheckError,
    RenameError,
    StructNotFoundError,
    Token,
    TokenGroup,
    row_type_for,
    table_for,
)
from kayino.prebuilt import PREBUILT_NORM_FUNCS, PREBUILT_NORMS
from kayino.schema import Table, Violation, columns_sql, ddl, kind_tag

"""
## Connecting and changing
"""


def connect(seed: bool = True) -> Registry:
    """A new registry. `seed` adds the prebuilt norm steps."""
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(ddl(TABLES))
    if seed:
        with change(db):
            for norm in PREBUILT_NORMS.values():
                insert(db, PrebuiltNorm.__name__, **CONVERTER.unstructure(norm))
    return db


@contextmanager
def change(db: Registry):
    """
    Apply the statements in the block atomically, or not at all: on a
    violation (SQLite's, or a custom check's) roll back and raise
    `RegistryCheckError`.
    """
    db.execute("SAVEPOINT change")
    try:
        yield db
        if violations := check(db):
            raise RegistryCheckError(violations)
    except BaseException as error:
        db.execute("ROLLBACK TO change")
        db.execute("RELEASE change")
        if isinstance(error, sqlite3.IntegrityError):
            raise RegistryCheckError([_violation_from_error(error)]) from error
        raise
    db.execute("RELEASE change")


def insert(db: Registry, struct_type_name: str, **fields) -> None:
    """Structure `fields` as the given struct type (defaults, types) and insert it."""
    struct = CONVERTER.structure(fields, row_type_for(struct_type_name))
    table = table_for(struct_type_name)
    insert_record(db, table, CONVERTER.unstructure(struct))


def insert_record(db: Registry, table: Table, record: dict) -> None:
    """
    Insert a row given as a dict of column values (from a struct, or straight
    from a CSV file). Missing columns are "" (union members without them);
    None lets SQLite assign the value (e.g. a text's id).
    """
    columns = [c for c in table.columns() if record.get(c, "") is not None]
    try:
        db.execute(
            f'INSERT INTO "{table.name}" ({columns_sql(columns)}) '
            f"VALUES ({', '.join('?' * len(columns))})",
            [record.get(c, "") for c in columns],
        )
    except sqlite3.IntegrityError as error:
        key = tuple(record.get(c) for c in table.key)
        raise RegistryCheckError([_violation_from_error(error, table, key)]) from error


def delete(db: Registry, struct_type_name: str, **where) -> None:
    """Delete matching rows (cascading to the rows they own). None found = error."""
    clause, params = _where(struct_type_name, where)
    table = table_for(struct_type_name)
    if not db.execute(f'DELETE FROM "{table.name}"{clause}', params).rowcount:
        raise StructNotFoundError(f"No {struct_type_name} with {where}")


"""
## Reading
"""


def select(db: Registry, struct_type_name: str, **where) -> tuple:
    """
    Rows as structs, filtered by column equality. Child tables come back in
    position order; other tables in insertion order.
    """
    table = table_for(struct_type_name)
    clause, params = _where(struct_type_name, where)
    order = columns_sql(table.key) if table.position else "rowid"
    columns = table.columns()
    cursor = db.execute(
        f'SELECT {columns_sql(columns)} FROM "{table.name}"{clause} ORDER BY {order}',
        params,
    )
    structure_as = table.row_type.__value__ if table.is_union else table.row_type
    return tuple(
        CONVERTER.structure(dict(zip(columns, row)), structure_as) for row in cursor
    )


def fetch(db: Registry, struct_type_name: str, **where) -> object:
    """Exactly one matching row."""
    found = select(db, struct_type_name, **where)
    if not found:
        raise StructNotFoundError(f"No {struct_type_name} with {where}")
    if len(found) > 1:
        raise DuplicateStructError(f"Expected one {struct_type_name} with {where}, got {found}")
    return found[0]


def _where(struct_type_name: str, where: dict) -> tuple[str, list]:
    """WHERE clause for column equality, plus `kind` when naming one union member."""
    table = table_for(struct_type_name)
    if table.is_union and struct_type_name not in (table.name, table.type_name):
        where = where | {"kind": kind_tag(row_type_for(struct_type_name))}
    if not where:
        return "", []
    return " WHERE " + " AND ".join(f'"{c}" = ?' for c in where), list(where.values())


"""
## Checking

SQLite reports a broken key, enum or type as soon as the statement runs
(`_violation_from_error`). References are deferred, so they are read with
`PRAGMA foreign_key_check` before the change is released. Custom checks run
only when everything else passes.
"""


def check(db: Registry) -> list[Violation]:
    if violations := _reference_violations(db):
        return violations
    return _patterns_compile(db) + _prebuilt_norms_exist(db)


def _reference_violations(db: Registry) -> list[Violation]:
    violations = []
    for table_name, rowid, parent, fk_id in db.execute("PRAGMA foreign_key_check"):
        table = TABLE_BY_NAME[table_name]
        column = next(
            row[3]
            for row in db.execute(f'PRAGMA foreign_key_list("{table_name}")')
            if row[0] == fk_id and row[1] == 0
        )
        ref_ = table.refs()[column]
        row = dict(
            zip(
                table.columns(),
                db.execute(
                    f'SELECT {columns_sql(table.columns())} FROM "{table_name}" WHERE rowid = ?',
                    (rowid,),
                ).fetchone(),
            )
        )
        value = row[column]
        target_kind = None
        if ref_.kind:
            target_kind = db.execute(
                f'SELECT "kind" FROM "{parent}" WHERE "{ref_.column}" = ?', (value,)
            ).fetchone()
        note = (
            f"{value!r} is a {target_kind[0]}, not a {ref_.kind}, in {parent}"
            if target_kind
            else f"{value!r} not found in {parent}.{ref_.column}"
        )
        violations.append(
            Violation(table_name, tuple(row[c] for c in table.key), column, "ref", note)
        )
    return violations


def _violation_from_error(
    error: sqlite3.IntegrityError,
    table: Table | None = None,
    key: tuple = (),
) -> Violation:
    message = str(error)
    if message.startswith("UNIQUE constraint failed: "):
        table_name = message.removeprefix("UNIQUE constraint failed: ").split(".")[0]
        table = TABLE_BY_NAME[table_name]
        return Violation(table.name, key, None, "key", f"duplicate key ({', '.join(table.key)})")
    table_name = table.name if table else "?"
    if message.startswith("CHECK constraint failed: "):
        name = message.removeprefix("CHECK constraint failed: ")
        kind, _, column = name.partition("_")
        if kind == "enum":
            return Violation(table_name, key, column, "enum", "not an allowed value")
        if kind == "required":
            return Violation(table_name, key, column, "required", "must not be empty")
    if message.startswith("cannot store"):
        column = message.rsplit(".", 1)[-1]
        return Violation(table_name, key, column, "type", message)
    return Violation(table_name, key, None, "file", message)


def _patterns_compile(db: Registry) -> list[Violation]:
    symbols = select(db, "symbols")
    violations = []
    for pattern in select(db, Pattern.__name__):
        try:
            fsa(pattern.value, symbols)
        except Exception as error:
            violations.append(
                Violation(
                    "symbols",
                    (pattern.name,),
                    "value",
                    "custom",
                    f"pattern {pattern.value!r} does not compile: {error}",
                )
            )
    return violations


def _prebuilt_norms_exist(db: Registry) -> list[Violation]:
    return [
        Violation("norm_steps", (norm.name,), "name", "custom", "no prebuilt function with this name")
        for norm in select(db, PrebuiltNorm.__name__)
        if norm.name not in PREBUILT_NORM_FUNCS
    ]


"""
## Renaming

`ON UPDATE CASCADE` carries a new key into every referencing row. What SQLite
cannot see is pattern strings: they mention symbol names as plain text, so a
symbol rename rewrites them, and is refused if it would change how any
pattern tokenizes (a new name can swallow neighbouring text by longest
match).
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


def _signature(tokens) -> tuple[tuple[str, str], ...]:
    """How a pattern string tokenizes, ignoring the empty splits."""
    return tuple((type(t).__name__, surface(t)) for t in tokens if surface(t))


def rename(db: Registry, struct_type_name: str, old_name: str, new_name: str) -> None:
    table = table_for(struct_type_name)
    if table.name not in RENAMEABLE_TABLES:
        raise RenameError(f"{struct_type_name} cannot be renamed")
    (key,) = table.key
    old_symbols = select(db, "symbols")
    with change(db):
        clause, params = _where(struct_type_name, {key: old_name})
        if not db.execute(
            f'UPDATE "{table.name}" SET "{key}" = ?{clause}', [new_name, *params]
        ).rowcount:
            raise StructNotFoundError(f"No {struct_type_name} with {key}={old_name!r}")
        if table.name != "symbols":
            return
        expected = {}
        for pattern in (s for s in old_symbols if isinstance(s, Pattern)):
            tokens = tokenize_str(pattern.value, old_symbols)
            renamed = lambda t: isinstance(t, (Token, TokenGroup, Pattern)) and t.name == old_name
            name = new_name if pattern.name == old_name else pattern.name
            value = "".join(new_name if renamed(t) else surface(t) for t in tokens)
            db.execute('UPDATE "symbols" SET "value" = ? WHERE "name" = ?', (value, name))
            expected[name] = tuple(
                (kind, new_name if kind != "AnonToken" and text == old_name else text)
                for kind, text in _signature(tokens)
            )
        new_symbols = select(db, "symbols")
        shifted = [
            pattern.name
            for pattern in new_symbols
            if isinstance(pattern, Pattern)
            and _signature(tokenize_str(pattern.value, new_symbols)) != expected[pattern.name]
        ]
        if shifted:
            raise RenameError(
                f"Renaming {old_name!r} to {new_name!r} would change how these "
                f"patterns tokenize: {', '.join(shifted)}"
            )
