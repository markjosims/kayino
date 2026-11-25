# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Your role: consultant, not developer

The user is the developer of this project. Claude acts only as a **software consultant**: give design critique, point out bugs, and suggest approaches when asked. Do not edit source or test files, add features, or refactor unless the user explicitly asks for that specific change. When suggesting code, show it in the reply rather than applying it.

## Project

Kayino is an interactive REPL for building a text normalization pipeline for a low-resource language. A pipeline (a *spec*) is an ordered list of norm steps:
1. prebuilt functions (`src/kayino/prebuilt.py`),
2. intab→outtab mappings,
3. `pynini.cdrewrite` contextual rewrite rules.

Normalized output is meant to be checked against an inventory of tokens and licit patterns (not implemented yet: `normalize` always returns `licit`). A project is saved as a folder of CSV files plus a generated Frictionless `datapackage.yaml`. See `README.md` for user-facing docs and `docs/plans/frictionless-refactor.md` for the design history.

## Commands

The project uses `uv` and Python 3.12 (`uv_build` backend).

```bash
uv sync                                   # install deps, including the dev group
uv run kayino                             # start the REPL (entry point: kayino:main)
uv run pytest                             # run all tests
uv run pytest tests/core_test.py::test_no_transitivity   # run a single test
uv run pytest -k "duplicate"              # filter parametrized cases by id
```

No linter or formatter is configured.

## Architecture

The data model is **relational and immutable**. Every stored struct is one table row (a frozen attrs class with no tuple/dict fields). The `Registry` is a `frozendict` mapping table name → tuple of rows. Every change returns a new registry, so a refused change simply leaves the old one in place.

**`schema.py`: generic table machinery** (knows nothing about kayino's structs).
- `ref(table, column="name", kind=None, on_delete="restrict"|"cascade")` declares a reference in attrs field metadata. `kind` restricts the target row's `kind`.
- `Table(name, row_type, key, position=None)`. `row_type` is a class or a PEP 695 union alias tagged on a `kind` field. `key` may span several columns: child tables have no id column, and their composite key *is* the constraint.
- `check_registry(registry, tables)` → `list[Violation]`: unique keys, resolving refs (with kind), `Literal` enums from resolved type hints.
- `make_converter` (cattrs, tagged unions on `kind`) and `check_tables` (sanity check of the declarations).

**`models.py`: the structs and `TABLES`, the single source of truth.** Tables: `symbols` (`Token | TokenGroup | Pattern`: one namespace for all three), `group_members`, `norm_steps` (`PrebuiltNorm | MappingNorm | CdrewriteNorm`), `mapping_rows`, `specs`, `spec_steps`, `spec_patterns`, `texts`. Nested data lives in child rows (`GroupMember`, `MappingRow`, `SpecStep`, `SpecPattern`) that reference their parent with `on_delete="cascade"`. Also: `CONVERTER`, `table_for(name)` (table, class or union name → `Table`), `row_type_for`, `NAME_TO_STRUCT` (the struct types the REPL addresses directly), and the error classes (`KayinoError`, `RegistryCheckError(violations)`, `KeyChangeError`, `RenameError`, ...). `NormalizedText` is derived output and is not a table.

**`structs.py`: registry operations.** `build_struct` (cattrs structure + insert), `insert_struct`, `remove_struct` (cascades to owned rows; anything still referencing the row makes the check fail, i.e. restrict), `replace_struct` (keeps position, refuses key changes), and `rename_struct` (updates referencing columns and pattern strings, and refuses renames that would change how a pattern tokenizes). All of them end in `_commit`, which runs `check()` and raises `RegistryCheckError`. `check()` = the generic checks, then `CUSTOM_CHECKS` (patterns compile, prebuilt names exist) only if those pass. Nested views: `group_members`, `mapping`, `spec_steps`, `spec_patterns`, `next_position`. `get_structs(name, registry)` accepts a table, class or union name.

**`acceptor.py`: tokenization and pattern → FSA.** `tokenize_str` splits by longest match over symbol names and regex operators. The pynini `SymbolTable` labels come from token order, starting at 1 (0 is epsilon). Patterns compile by recursive descent. Groups and pattern references are not compiled yet (`_group_or_pattern_to_fst` is a stub). It cannot import `structs` (import cycle), so it reads `registry["symbols"]` directly.

**`core.py`: the pipeline.** `normalize(text, spec, registry)` applies the spec's steps in order with `apply_step(text, step, registry)`. Mappings are applied in **one regex pass**, longest intab first (`sorted_intab_regex`), so rewrites do not chain; `tests/test_cases/mapping_tests.jsonl` pins this down.

**`store.py`: the project folder.** `dump`/`load` use one CSV per table, via cattrs with no per-table code. Dump groups child rows by parent and renumbers positions. `to_datapackage()`/`datapackage_yaml()` generate the Frictionless descriptor from `TABLES`; it is written on save but **never read back** (validation always uses the generated one). `validate_project(path)` = Frictionless on the raw files (`Violation(kind="file"|"key"|"ref"|"enum")`), then `check()`. `export` writes normalized outputs to a CSV outside the package. Frictionless is too slow for every command (~100–250 ms), which is why per-command checking is the in-memory `check()` (~1–2 ms).

**`repl/`: the shell.**
- `grammar.py` builds one prompt_toolkit grammar from per-struct regex templates. Struct shorthands: `tok`, `tkg`, `pat`, `map`, `rwr`, `spec`, `step`, `text`.
- `commands.py` maps each verb to a `handle_*(kwargs, state) -> (message, new_state)`. `repl_turn` catches `KayinoError` and reports it. Any other exception ends the session.
- `models.py`: `ReplState(registry, active_spec, project_dir)`.
- To add a command: add a grammar template, write a `handle_*` and register it in `_verb_to_func`.

Known bugs are marked with `# BUG:` comments in the source. `README.md` lists the user-visible ones.

## Tests

Most tests are parametrized from JSONL files in `tests/test_cases/`. Add cases by adding lines.
- `mapping_tests.jsonl`, `prebuilt_tests.jsonl` → `core_test.py`.
- `command_tests.jsonl`, `argword_tests.jsonl` → `grammar_test.py` (parsed kwargs per input line).
- `registry_violations.jsonl` → `check_test.py` and `store_test.py`. Each case has `rows` per table, `expected` (what `check()` reports, as `[table, kind, column]`) and `frictionless` (what Frictionless reports for the same rows on disk; kind-restricted refs and custom checks are `check()`-only).
- `datapackage.snapshot.yaml` → `store_test.py`. Regenerate it after an intentional model change (the command is in the test's docstring).

`commands_test.py`, `check_test.py` and `store_test.py` also drive the REPL end to end through `repl_turn`.
