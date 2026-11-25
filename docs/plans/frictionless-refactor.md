# Plan: Row-shaped structs with a schema they generate themselves

Branch: `frictionless-refactor`

## Goal

Replace the hand-written constraint plumbing in `structs.py` (hook registries,
id allocation, reindexing, dependency checks) with constraints **declared once,
on the attrs classes**. A kayino project becomes a folder of human-readable
CSV files that can be diffed in git and edited in a spreadsheet.

There is one source of truth: the attrs classes, plus their field metadata and
a table list. Everything else is derived from it:

```
          attrs classes + field metadata + TABLES      (src/kayino/schema.py)
              │                │                 │
              ▼                ▼                 ▼
   in-memory checker   datapackage.yaml     CSV dump/load
   (every command)     (generated on save;  (cattrs unstructure /
                        Frictionless checks  structure, no hand mapping)
                        files at load/save)
```

The in-memory model stays immutable: frozen structs in a `Registry`, with
state changes through `evolve`. Rollback costs nothing: when a check fails,
the handler raises and the REPL keeps the old state.

## Non-goals

- No live database, and no ORM.
- No change to normalization semantics (`core.py`).
- No REPL grammar changes beyond the new persistence commands.

## Key design change: structs have the same shape as table rows

Most of the duplication in the earlier draft came from nested structs
(`TokenGroup.members: tuple`, `MappingNorm.data: frozendict`,
`NormalizationSpec.steps: tuple`) being translated into flat tables. Instead,
make **every struct one row**, and turn nested data into child rows:

| Table | Row class(es) | Key | Notes |
|---|---|---|---|
| `symbols` | `Token \| TokenGroup \| Pattern` (tagged on `kind`) | `name` | one table, so names are unique across all three kinds |
| `group_members` | `GroupMember(group, position, token)` | `group, position` | replaces `TokenGroup.members` |
| `norm_steps` | `PrebuiltNorm \| MappingNorm \| CdrewriteNorm` (tagged on `kind`) | `name` | `CdrewriteArgs` fields move onto `CdrewriteNorm` |
| `mapping_rows` | `MappingRow(norm, intab, outtab)` | `norm, intab` | replaces `MappingNorm.data`; also forbids duplicate intabs |
| `specs` | `NormalizationSpec(name, description)` | `name` | |
| `spec_steps` | `SpecStep(spec, position, step)` | `spec, position` | replaces `spec.steps` |
| `spec_patterns` | `SpecPattern(spec, pattern)` | `spec, pattern` | replaces `spec.licit_patterns` |
| `texts` | `Text(id, string, source_type, source_value)` | `id` | |

Code that wants the nested view (the acceptor, `core.normalize`, the REPL
`list` output) gets it from small query helpers, e.g.
`group_members(registry, name) -> tuple[str, ...]` and
`mapping(registry, norm) -> dict[str, str]`. Those helpers are the **only**
place the nested shape exists.

Positions may have gaps in memory (append = `max + 1`, removal leaves a gap).
They are renumbered `0..n` on dump, so a human editor can delete CSV rows
without fixing numbers. The position column, not the row order, decides the
order: to reorder by hand, edit the numbers.

## How constraints are declared

Constraints that apply to a single column go in attrs field metadata.
Table-level facts go in one `TABLES` tuple. Sketch:

```python
# src/kayino/schema.py
from attrs import field, frozen

@frozen
class Ref:
    table: str
    column: str = "name"
    kind: str | None = None                       # restrict the target row's `kind`
    on_delete: Literal["restrict", "cascade"] = "restrict"

def ref(table, column="name", *, kind=None, on_delete="restrict", **kw):
    return field(metadata={"ref": Ref(table, column, kind, on_delete)}, **kw)

@frozen
class Table:
    name: str
    row_type: type | TypeAliasType               # a class or a tagged union
    key: tuple[str, ...]
    position: str | None = None                  # column renumbered on dump

# src/kayino/models.py
@frozen
class GroupMember:
    group: str = ref("symbols", kind="group", on_delete="cascade")
    position: int
    token: str = ref("symbols", kind="token")

TABLES = (
    Table("symbols", Symbol, key=("name",)),
    Table("group_members", GroupMember, key=("group", "position"), position="position"),
    ...
)
```

Everything else is derived from the classes, not declared:

- **Column names and types** come from `attrs.fields()`: `str` → `string`,
  `int` → `integer`.
- **Enums** come from `Literal[...]` annotations, including the `kind` tag of a
  union.
- **Which columns a union table has** is the union of its members' fields.
  A column a member doesn't use is written as `""`.

How the hooks in today's code map onto this:

| Current hook / code | Replacement |
|---|---|
| `_array_ids_unique` across Token/TokenGroup/Pattern | `symbols` key `name` |
| `_array_ids_contiguous`, `_reindex_tokens`, `_get_available_consecutive_key`, `Token.id` | gone. The symbol table index is derived from order at FSA compile time |
| `_check_depending_structs`, `_pre_remove_hooks`, `_post_remove_hooks` | `ref(...)` with `on_delete` (`restrict` by default) |
| `_struct_mutation_hooks` (append/pop on tuples) | gone. Appending a step or member is inserting a row |
| `_construction_hooks` / `_rename_field` | cattrs `override(rename=...)` where a REPL arg name differs from the field name |
| `_field_hooks[Text]["id"]` | small `next_id(registry, "texts")` helper |
| `_validate_pattern_compiles`, `_struct_validation_hooks` | entry in a plain `CUSTOM_CHECKS` list |
| `NAME_TO_STRUCT`, `TYPE_TO_UNION`, `resolve_struct_type_*` | derived from `TABLES` |

`on_delete="cascade"` is for rows that belong to a parent: a group's members,
a mapping's rows, a spec's steps and patterns. Deleting the parent removes
them. Everything else is `restrict`: removing a token that a group still uses
fails with a message naming the group.

## Checking

**`check(registry) -> list[Violation]`** runs after every change. It is generic
over `TABLES` and takes ~1–2 ms at realistic sizes (Phase 0):

1. key uniqueness for each table
2. for each `ref` field, the target row exists and, if `kind` is set, has that kind
3. `Literal` enum membership. This covers structs built by direct
   constructor calls; cattrs already enforces it on structure.
4. `CUSTOM_CHECKS`, which run only if 1–3 pass:
   - every pattern compiles to an FSA
   - prebuilt norm names exist in `PREBUILT_NORM_FUNCS`

**At the file boundary** (`load`, `save`, `validate`), the generated
`datapackage.yaml` is checked with Frictionless. This catches the things only
the raw files can get wrong: bad integers, missing columns, a malformed CSV.
Then `check()` runs on the loaded registry. Frictionless can't express
kind-restricted refs or the custom checks, so those are always left to
`check()`. Nothing is expressed twice by hand.

`Violation(table, key, column, kind, note)` is the single error shape for
both paths. The REPL prints one line per violation, e.g.
`group_members (V, 2).token: "ʔ" is not a token in symbols`.

## Target on-disk layout

```
my-language/
  datapackage.yaml        # GENERATED on save. Never hand-edited (header comment says so)
  symbols.csv             # name, kind, value
  group_members.csv       # group, position, token
  norm_steps.csv          # name, kind, description, input_str, output_str, left_context, right_context
  mapping_rows.csv        # norm, intab, outtab
  specs.csv               # name, description
  spec_steps.csv          # spec, position, step
  spec_patterns.csv       # spec, pattern
  texts.csv               # id, string, source_type, source_value
```

Empty cells: kayino reads the CSVs with the `csv` module, so `""` always
comes back as `""` (an empty outtab means "delete", an empty context means
"any context"). The generated schema keeps Frictionless's default (an empty
cell counts as missing) and marks keys, references, enums and integers
`required`. Empty names, intabs, references and kinds are refused; empty
descriptions, outtabs, values and contexts are fine. (This replaces the
Phase 0 idea of `missingValues: []`, which would have let empty names
through. See Phase 4.)

## Phases

### Phase 0: Spike (done 2026-11-23, frictionless 5.21.0)

- Inline-data packages validate PKs, cross-resource FKs and enums. Every
  violation case tested was caught.
- Running full Frictionless validation on every command is too slow:
  ~90 ms fixed overhead, ~260 ms with 5k texts, ~1.6 s with 50k texts.
  Equivalent hand-written Python checks take ~1.4 ms. Hence the generic
  in-memory checker for every command, with Frictionless only at the file
  boundary.
- `missingValues: []` keeps `""` through a round trip. By default it reads
  back as `None`. (Superseded in Phase 4: kayino reads CSVs itself, so this
  only affected validation, where it made things less strict.)
- A lone U+0301, NFD vs. NFC pairs, leading and trailing spaces, `"`, and the
  date-like value `1-2` all survive a CSV round trip.
- `report.flatten(["rowNumber", "fieldName", "type", "note"])` gives
  per-violation rows that convert easily to `Violation`.

### Phase 1: Standalone bug fixes (small, land first)

- [x] Build the pynini `SymbolTable` with keys starting at **1**. Today the
      first token gets id `0`, which pynini treats as the epsilon label.
- [x] `acceptor._token_regex` and `get_token_group_map` read `.string` on
      `TokenGroup`/`Pattern`, which only have `name`. The same bug was in
      the uniqueness hook in `structs.py`, which made it impossible to add
      any TokenGroup or Pattern. `tokenize_str` also now recognizes pattern
      names.
- [x] Tests (`tests/acceptor_test.py`): the first token compiles to a
      non-epsilon arc, group and pattern names are recognized by the
      tokenizer, and names are unique across the three types.

### Phase 2: `schema.py` and flat models

- [x] `Ref`, `ref()`, `Table` in `schema.py`. `TABLES`, `CONVERTER`,
      `table_for()` and `row_type_for()` in `models.py`. `TYPE_TO_UNION` and
      `resolve_struct_type_name` are gone. `NAME_TO_STRUCT` remains as the
      explicit list of struct types the REPL addresses (child row types are
      left out).
- [x] Flatten the models. `Token.string` → `Token.name`. Removed
      `Token.id`, `CdrewriteArgs` (its fields are now on `CdrewriteNorm` as
      `input_str`/`output_str`/`left_context`/`right_context`, matching the
      grammar's arg names), and the tuple/dict fields. New child row types:
      `GroupMember`, `MappingRow`, `SpecStep`, `SpecPattern`.
      `NormalizedText` is no longer a table.
- [x] cattrs converter with tagged unions on `kind`. No configuration was
      needed to drop unused columns: cattrs ignores extra keys by default.
- [x] `check_tables()` sanity check, run in a test (`tests/schema_test.py`)
      rather than at import time.
- [x] Query helpers in `structs.py`: `group_members`, `mapping`,
      `spec_steps`, `spec_patterns`, plus `next_position`.
- [x] Minimal caller updates so everything keeps working:
  - `acceptor` reads the `symbols` table and uses a `surface()` helper.
  - `core.apply_step` now takes the registry.
  - `commands`: `add tkg`/`add map` insert child rows, `spec add/rm` insert
    and remove rows, `list` shows nested views as extra columns.
  - Grammar Token args are now `name`.
  - Hooks whose fields no longer exist were removed. The rest wait for
    Phase 3.

**Interim cascade:** flattening made removing a parent leave its child rows
behind. If a parent with the same name was added again, it inherited the old
children. `remove_struct` now removes rows whose `ref` is
`on_delete="cascade"`, generically over `TABLES` (`_cascade_children`).
`update_struct` opts out (`cascade=False`), because it is implemented as
remove + re-add. `on_delete="restrict"` is still **not** enforced; that is
Phase 3's `check()`, which also replaces this interim code.

### Phase 3: Checker, then delete the hooks (done)

- [x] `check()` = the generic `schema.check_registry(registry, TABLES)` (keys,
      refs including `kind`, `Literal` enums), then `structs.CUSTOM_CHECKS`
      (patterns compile, prebuilt names exist) only if the generic checks
      pass. Violations are `schema.Violation(table, key, column, kind, note)`.
      Enum values are read from the resolved type hints, so string
      annotations work.
  - **Dropped** the planned "cdrewrite `input_str` non-empty" check: empty
    input is a valid *insertion* rule (`'' -> h / a _ a` gives `aha` in
    pynini).
- [x] `insert_struct` / `remove_struct` / `replace_struct`, each ending in
      `_commit()`, which raises `RegistryCheckError` and leaves the old
      registry untouched.
  - `remove_struct` removes the struct plus everything it owns through
    `on_delete="cascade"` refs (recursively), then checks. Any remaining
    reference makes the check fail, which is how restrict works.
  - `replace_struct` keeps the row's position and raises `KeyChangeError`
    on a key change.
- [x] `rename_struct` and a REPL `rename|mv STRUCT OLD NEW` command, following
      the five steps planned. `RENAMEABLE_TABLES` is derived from `TABLES`
      (single-column keys that some `ref` targets) and comes out as
      `{symbols, norm_steps, specs}`. Renaming the active spec also
      updates `ReplState.active_spec`.
- [x] Callers were already updated in Phase 2.
- [x] Deleted: every hook registry, `construct_fields`, `_rename_field`,
      `_parse_members`, `mutate_struct` and the append/pop helpers,
      `validate_struct(s_in_registry)`, `_check_depending_structs`, the
      interim `_cascade_children`, `register_struct`/`update_struct`
      (renamed to `insert_struct`/`replace_struct`), and
      `MalformedStructArrayError`, `StructDependencyError` and
      `FsaCompilationError`. Text ids come from a plain `next_id()` call in
      `build_struct`. `structs.py`: 534 → 438 lines, with rename and the
      nested views included.
- [x] Tests: `tests/check_test.py` with
      `tests/test_cases/registry_violations.jsonl` (18 cases covering every
      constraint kind), refused REPL commands leaving the state unchanged,
      restrict vs cascade, replace, and rename (groups and pattern strings,
      spec steps and mapping rows, the active spec, name collisions,
      tokenization shifts, unsupported tables). Grammar fixtures cover
      `rename`/`mv`.

Not in scope, flagged with `# BUG:`: `core.byte_star` only accepts `""`, so
every cdrewrite step currently returns `""`.

### Phase 4: Persistence (done)

- [x] `store.to_datapackage()` / `datapackage_yaml()`: generated from
      `TABLES` (column types and enums from the resolved type hints, keys,
      plain FKs for every `ref`). It has a "do not edit" header. Snapshot
      test: `tests/test_cases/datapackage.snapshot.yaml`. Frictionless
      accepts it as a valid descriptor.
  - Validation always uses the descriptor generated by the current code
    and **never reads `datapackage.yaml` from disk**, so an edited or stale
    copy can't loosen the checks.
- [x] `store.dump(registry, path)` / `store.load(path)`: cattrs
      unstructure/structure per table with no per-table code. Dump groups
      child rows by parent, sorts them by position and renumbers them.
      CSVs use `\n` line endings to keep git diffs clean.
  - **Encoding: plain UTF-8, no BOM** (Frictionless's and git's default).
    Not tested in Excel. Older Excel may show mojibake when opening
    UTF-8 CSVs without a BOM; LibreOffice asks for the encoding. Revisit if
    users hit it.
- [x] Two-layer validation (`store.validate_project`): Frictionless on the
      raw files, converted to `Violation` (`file` kind for type errors and
      missing files), then `check()` on the structured registry.
      `store.load` raises `RegistryCheckError` with every violation.
- [x] Consistency test: every case in `registry_violations.jsonl` has a
      `"frictionless"` field with what Frictionless should report on disk.
      The test dumps each case without renumbering and compares.
- [x] REPL commands (grammar + handlers):
  - `init DIR`: creates a new project **from the current session** (in a
    fresh session, just the prebuilt norms) and refuses a non-empty folder.
    This never throws away unsaved work.
  - `save [DIR]`: refuses a non-empty folder that isn't a kayino project.
    Re-validates the written folder and reports problems without blocking.
  - `load DIR`: replaces the session. **No unsaved-changes warning yet.**
    A failed load keeps the session.
  - `validate`: validates the project folder **on disk** (useful after
    editing CSVs by hand).
  - `ReplState.project_dir`.
- [x] `export SPEC PATH`: normalizes **all** texts with a spec and writes CSV
      (`text_id, spec_name, input, string, status, payload`). It refuses to
      overwrite a file belonging to the data package. No per-text
      selection or per-step intermediate outputs yet.
- [x] Removed the `.cache` mkdir that ran on import.
- [x] Decided: **Frictionless is a required dependency** (with `pyyaml`).

### Phase 5: Docs (done)

- [x] `CLAUDE.md`: rewrote Project, Commands, Architecture and Tests for the
      current modules. The "consultant, not developer" role section is
      unchanged.
- [x] `README.md` (was empty): concepts, REPL commands (each documented
      form checked against the running REPL), the project folder, editing
      CSVs by hand, and known issues.
- Found while checking commands, now listed in the README's known issues:
  `add rwr ...` crashes the REPL (the grammar has no slot for the required
  `name`), and quoted pattern values keep their quote characters.

## Decisions

1. **One `symbols` table** for tokens, groups and patterns, as a tagged union
   on `kind`.
2. **`NormalizedText` is not persisted.** Only `export` writes it.
3. **Validation timing:** the generic in-memory checker on every command,
   and Frictionless at load/save/`validate` (Phase 0 timings).
4. **Single source of truth:** attrs classes with field metadata plus
   `TABLES`. `datapackage.yaml` is generated, never edited.
5. **Key changes go only through a separate `rename` command.** It updates
   every referencing row and pattern string. `replace` rejects key changes.
   (Phase 3)

## Open questions

- `export`: selecting texts, per-step intermediate outputs, JSONL as an
  alternative format.
- `load` when the session has unsaved changes: warn, or refuse without
  `load! DIR`? Needs a "dirty" flag (e.g. the registry as last saved,
  kept on `ReplState`).
- CLDF import: out of scope here, but the flat tables make it easy later.

## Risks

- **Adding a token can also shift tokenization.** The rename guard covers
  renames, but `add tok ab` when a pattern contains `ab` (previously `a` +
  `b`) silently changes that pattern's meaning. It still compiles, so
  `check()` passes. The same guard (comparing pattern signatures before
  and after) could run on every symbol insert.

- **Spreadsheet editors can silently change text:** normalizing Unicode,
  trimming whitespace, or turning values like `1-2` into dates. Mitigation:
  `load` always validates, and the README warns about it.
- **The generic checker grows into a second Table Schema engine.**
  Mitigation: it handles exactly keys, refs, enums and `CUSTOM_CHECKS`.
  Anything about raw file values (types, formats) is left to Frictionless
  at the boundary.
- **The nested-view helpers get called in hot paths** (the acceptor
  rebuilding group maps on every tokenize). Mitigation: cache them on the
  registry's identity, the same way `acceptor.py` already does with
  `lru_cache`.
