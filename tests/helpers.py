"""
Shared test helpers.
"""

import csv
from pathlib import Path

from kayino.models import CONVERTER, TABLE_BY_NAME, TABLES
from kayino.prebuilt import PREBUILT_NORMS
from kayino.repl.commands import repl_turn
from kayino.repl.models import ReplState
from kayino.store import DATAPACKAGE_FILE, datapackage_yaml
from kayino.structs import select


def snapshot(db) -> dict[str, tuple]:
    """Every table's rows, for comparing registries by value."""
    return {table.name: select(db, table.name) for table in TABLES}


def write_project(path: Path, rows: dict[str, list[dict]]) -> None:
    """
    Write a project folder straight from row dicts, bypassing every check
    (so invalid data can be written). The prebuilt norm steps are always
    included, as `init` would.
    """
    path.mkdir(parents=True, exist_ok=True)
    rows = dict(rows)
    rows["norm_steps"] = [
        CONVERTER.unstructure(norm) for norm in PREBUILT_NORMS.values()
    ] + list(rows.get("norm_steps", []))
    for table in TABLES:
        with open(path / f"{table.name}.csv", "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=TABLE_BY_NAME[table.name].columns())
            writer.writeheader()
            for row in rows.get(table.name, []):
                writer.writerow({c: row.get(c, "") for c in table.columns()})
    (path / DATAPACKAGE_FILE).write_text(datapackage_yaml(), encoding="utf-8")


def run(commands: list[str], grammar, state: ReplState | None = None):
    """Run REPL commands; returns (messages as str, final state)."""
    state = state or ReplState()
    messages = []
    for command in commands:
        message, state = repl_turn(command, state, grammar)
        messages.append(message if isinstance(message, str) else "".join(t for _, t in message))
    return messages, state
