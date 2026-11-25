"""
Tests for the on-disk project format: the generated datapackage, dump/load,
validation at the file boundary, export, and the REPL project commands.
"""

import csv
from pathlib import Path

import pytest
from frictionless import Package

from check_test import VIOLATIONS, registry_from_case
from kayino.models import RegistryCheckError
from kayino.repl.commands import repl_turn
from kayino.repl.grammar import create_grammar
from kayino.repl.models import ReplState
from kayino.store import (
    DATAPACKAGE_FILE,
    ProjectError,
    datapackage_yaml,
    dump,
    file_violations,
    load,
    to_datapackage,
    validate_project,
)
from kayino.structs import build_struct, group_members, init_registry

SNAPSHOT = Path(__file__).parent / "test_cases" / "datapackage.snapshot.yaml"


def test_datapackage_matches_snapshot():
    """
    If this fails after an intentional model change, regenerate with:
    uv run python -c "from kayino.store import datapackage_yaml; \
open('tests/test_cases/datapackage.snapshot.yaml','w').write(datapackage_yaml())"
    """
    assert datapackage_yaml() == SNAPSHOT.read_text()


def test_datapackage_is_a_valid_descriptor():
    assert Package.validate_descriptor(to_datapackage()).valid


def _sample_registry():
    registry = init_registry()
    for name in ["a", "b", "ch"]:
        _, registry = build_struct("Token", registry, name=name)
    _, registry = build_struct("TokenGroup", registry, name="<V>")
    for position, token in enumerate(["a", "ch"]):
        _, registry = build_struct(
            "GroupMember", registry, group="<V>", position=position, token=token
        )
    _, registry = build_struct("Pattern", registry, name="<P>", value="ab")
    _, registry = build_struct("MappingNorm", registry, name="m", description="d")
    for intab, outtab in [("x", ""), ("́", ""), (" ", "_"), ("a ", " b"), ('q"', "1-2")]:
        _, registry = build_struct("MappingRow", registry, norm="m", intab=intab, outtab=outtab)
    _, registry = build_struct(
        "CdrewriteNorm", registry, name="r", input_str="", output_str="h", left_context="a"
    )
    _, registry = build_struct("NormalizationSpec", registry, name="s")
    _, registry = build_struct("SpecStep", registry, spec="s", position=0, step="lowercase")
    _, registry = build_struct("SpecStep", registry, spec="s", position=1, step="m")
    _, registry = build_struct("SpecPattern", registry, spec="s", pattern="<P>")
    _, registry = build_struct("Text", registry, string=" Hi, \"there\" é ")
    return registry


def test_round_trip(tmp_path):
    registry = _sample_registry()
    dump(registry, tmp_path)
    assert validate_project(tmp_path) == []
    assert load(tmp_path) == registry


def test_dump_renumbers_positions(tmp_path):
    registry = init_registry()
    for name in ["a", "b", "c"]:
        _, registry = build_struct("Token", registry, name=name)
    _, registry = build_struct("TokenGroup", registry, name="<V>")
    for position, token in [(7, "c"), (2, "a"), (5, "b")]:
        _, registry = build_struct(
            "GroupMember", registry, group="<V>", position=position, token=token
        )
    dump(registry, tmp_path)
    with open(tmp_path / "group_members.csv", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [(r["position"], r["token"]) for r in rows] == [("0", "a"), ("1", "b"), ("2", "c")]
    assert group_members(load(tmp_path), "<V>") == ("a", "b", "c")


@pytest.mark.parametrize("case", VIOLATIONS, ids=[case["id"] for case in VIOLATIONS])
def test_frictionless_agrees_with_check(case, tmp_path):
    """
    The generated schema enforces the same keys, plain refs and enums as
    check() does in memory. (Kind-restricted refs and custom checks are
    check()-only, which the "frictionless" field of each case records.)
    """
    dump(registry_from_case(case), tmp_path, renumber=False)
    found = {(v.table, v.kind, v.column) for v in file_violations(tmp_path)}
    assert found == {tuple(expected) for expected in case["frictionless"]}


def _edit_csv(path: Path, table: str, transform) -> None:
    file = path / f"{table}.csv"
    file.write_text(transform(file.read_text(encoding="utf-8")), encoding="utf-8")


@pytest.mark.parametrize(
    "table, transform, expected",
    [
        ("symbols", lambda text: text + "a,token,\n", ("symbols", "key", None)),
        ("group_members", lambda text: text.replace(",1,ch", ",x,ch"), ("group_members", "file", "position")),
        ("group_members", lambda text: text.replace(",ch\n", ",zz\n"), ("group_members", "ref", "token")),
        ("symbols", lambda text: text.replace("<P>,pattern,ab", "<P>,pattern,abzz"), ("symbols", "custom", "value")),
        ("group_members", lambda text: text.replace(",a\n", ",<P>\n"), ("group_members", "ref", "token")),
    ],
    ids=["duplicate token", "bad integer", "dangling ref", "uncompilable pattern", "member is a pattern"],
)
def test_load_refuses_hand_edited_problems(tmp_path, table, transform, expected):
    dump(_sample_registry(), tmp_path)
    _edit_csv(tmp_path, table, transform)
    with pytest.raises(RegistryCheckError) as error:
        load(tmp_path)
    assert expected in {(v.table, v.kind, v.column) for v in error.value.violations}


@pytest.mark.parametrize(
    "table, transform, column",
    [
        ("symbols", lambda text: text + ",token,\n", "name"),
        ("mapping_rows", lambda text: text + "m,,z\n", "intab"),
        ("group_members", lambda text: text.replace(",ch\n", ",\n"), "token"),
        ("symbols", lambda text: text.replace("a,token,", "a,,"), "kind"),
    ],
    ids=["empty name", "empty intab", "empty reference", "empty kind"],
)
def test_load_refuses_empty_required_cells(tmp_path, table, transform, column):
    dump(_sample_registry(), tmp_path)
    _edit_csv(tmp_path, table, transform)
    with pytest.raises(RegistryCheckError) as error:
        load(tmp_path)
    assert (table, column) in {(v.table, v.column) for v in error.value.violations}


def test_load_ignores_edited_datapackage(tmp_path):
    dump(_sample_registry(), tmp_path)
    (tmp_path / DATAPACKAGE_FILE).write_text("resources: []\n")
    _edit_csv(tmp_path, "symbols", lambda text: text + "a,token,\n")
    with pytest.raises(RegistryCheckError):
        load(tmp_path)


def test_missing_csv_is_reported(tmp_path):
    dump(_sample_registry(), tmp_path)
    (tmp_path / "texts.csv").unlink()
    assert ("texts", "file", None) in {
        (v.table, v.kind, v.column) for v in validate_project(tmp_path)
    }


def test_load_refuses_non_project(tmp_path):
    with pytest.raises(ProjectError):
        load(tmp_path)


"""
## REPL project commands
"""


@pytest.fixture(scope="module")
def grammar():
    return create_grammar()


def run(commands: list[str], grammar, state: ReplState | None = None):
    state = state or ReplState()
    messages = []
    for command in commands:
        message, state = repl_turn(command, state, grammar)
        messages.append(str(message))
    return messages, state


def test_init_save_load_validate(tmp_path, grammar):
    project = tmp_path / "proj"
    messages, state = run(
        [f"init {project}", "add tok a", "add tkg <V> a", "save"], grammar
    )
    assert state.project_dir == project
    assert messages[-1] == f"Saved to {project}"

    messages, loaded = run([f"load {project}", "validate"], grammar)
    assert loaded.registry == state.registry
    assert messages[-1] == f"{project} is valid"

    _edit_csv(project, "group_members", lambda text: text.replace(",a\n", ",zz\n"))
    messages, _ = run(["validate"], grammar, loaded)
    assert "has problems" in messages[-1]
    assert "group_members.token" in messages[-1]


def test_init_refuses_non_empty_folder(tmp_path, grammar):
    (tmp_path / "notes.txt").write_text("hi")
    messages, state = run([f"init {tmp_path}"], grammar)
    assert "ProjectError" in messages[0]
    assert state.project_dir is None


def test_save_needs_a_folder(grammar):
    messages, _ = run(["save"], grammar)
    assert "ProjectError" in messages[0]


def test_save_refuses_foreign_folder(tmp_path, grammar):
    (tmp_path / "notes.txt").write_text("hi")
    messages, _ = run([f"save {tmp_path}"], grammar)
    assert "not a kayino project" in messages[0]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


def test_failed_load_keeps_session(tmp_path, grammar):
    _, state = run(["add tok a"], grammar)
    messages, new_state = run([f"load {tmp_path}"], grammar, state)
    assert "ProjectError" in messages[0]
    assert new_state is state


def test_export(tmp_path, grammar):
    project = tmp_path / "proj"
    out = tmp_path / "out" / "normalized.csv"
    texts = tmp_path / "texts.txt"
    texts.write_text("ABA\nbab\n", encoding="utf-8")
    messages, _ = run(
        [
            f"init {project}",
            f"read {texts}",
            "add map m a > o",
            "add spec s",
            "spec act s",
            "spec add step lowercase",
            "spec add step m",
            f"export s {out}",
        ],
        grammar,
    )
    assert messages[-1] == f"Exported 2 normalized texts to {out}"
    with open(out, encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [(r["input"], r["string"], r["spec_name"]) for r in rows] == [
        ("ABA", "obo", "s"),
        ("bab", "bob", "s"),
    ]
    # outputs are never part of the project
    assert not (project / "normalized.csv").exists()


def test_export_refuses_package_files(tmp_path, grammar):
    project = tmp_path / "proj"
    messages, _ = run(
        [f"init {project}", "add spec s", f"export s {project / 'symbols.csv'}"], grammar
    )
    assert "ProjectError" in messages[-1]
    assert (project / "symbols.csv").read_text().startswith("name,kind,value")
