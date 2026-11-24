# Plan: Frictionless-backed registry

Branch: `frictionless-refactor`

## Goal

Replace the hand-written constraint plumbing in `structs.py` (hook registries,
id allocation, reindexing, dependency checks) with a declarative
[Frictionless Data Package](https://datapackage.org/) schema, and make a
kayino project a folder of human-readable CSV files that can be validated,
diffed in git, and edited in a spreadsheet.

The in-memory model stays as it is: frozen attrs structs in an immutable
`Registry`, with state changes through `evolve`. Frictionless serves only as
the **constraint checker** and the **on-disk format**. Because the registry is
immutable, rollback costs nothing: when validation fails, the handler raises
and the REPL keeps the old state.

## Non-goals

- No live database. Data sizes are small enough to validate the whole
  registry after every change. Phase 0 tests this assumption.
- No change to normalization semantics (`core.py`) or the REPL grammar beyond
  the new persistence commands.

## Target on-disk layout

```
my-language/
  datapackage.yaml        # schema: fields, primary keys, foreign keys, constraints
  symbols.csv             # name, kind (token|group|pattern), value
  group_members.csv       # group, position, token
  norm_steps.csv          # name, kind, description, input, output, left_context, right_context
  mapping_rows.csv        # norm, intab, outtab
  specs.csv               # name, description
  spec_steps.csv          # spec, position, step
  spec_patterns.csv       # spec, pattern
  texts.csv               # id, string, source_type, source_value
```

Constraints in the schema:

| Current hook / code | Schema replacement |
|---|---|
| `_array_ids_unique` across Token/TokenGroup/Pattern | `symbols.primaryKey: [name]` (one table, so uniqueness across all three kinds comes free) |
| `_array_ids_contiguous`, `_reindex_tokens`, `_get_available_consecutive_key` | removed. Tokens have no stored id (see Phase 1) |
| `_check_depending_structs` (never actually wired up) | FKs: `group_members.token → symbols.name`, `spec_steps.step → norm_steps.name`, `spec_patterns.pattern → symbols.name`, `mapping_rows.norm → norm_steps.name`, `*.spec → specs.name` |
| no duplicate intab within one mapping (currently unchecked) | `mapping_rows.primaryKey: [norm, intab]` |
| `kind` literal on NormStep subtypes | `norm_steps.kind` enum constraint |
| `_get_available_nonconsecutive_key` for Text | stays as a small helper. `texts.primaryKey: [id]` checks it |

What stays as custom Python checks, run after Frictionless passes:

1. A `group_members.token` must point to a symbol of `kind == token`, and a
   `spec_patterns.pattern` to one of `kind == pattern`. Table Schema FKs
   can't filter by kind.
2. Required columns for each kind in `norm_steps`: cdrewrite rows need
   `input`, and prebuilt names must exist in `PREBUILT_NORM_FUNCS`.
3. Every pattern compiles to an FSA (`_validate_pattern_compiles`).

Ordered tuples (`TokenGroup.members`, `spec.steps`) are stored with a
`position` column. Rows are sorted by position on load and renumbered on dump,
so a human editor can reorder rows without fixing numbers by hand.

## Phases

### Phase 0: Spike (throwaway, in a scratch dir; nothing merged)

Answer these before committing to the design:

- [ ] Can a `Package` built from **inline data** (lists of dicts, no files)
      validate FKs across resources? Or do we have to write temp files?
- [ ] What does validation cost with realistic sizes (~200 symbols,
      ~20 norm steps with ~500 mapping rows, ~5k texts)? Target: well under
      100 ms per command. If it's slower, validate texts only on save, or
      validate on save only and run the cheap custom checks per command.
- [ ] **Empty strings vs. missing values.** By default Frictionless treats
      `""` as null, but an empty `outtab` means "delete" and an empty cdrewrite
      context means "any context". Confirm that `missingValues: []` on those
      schemas keeps `""` intact through a dump→load→validate round trip.
- [ ] Do whitespace and combining characters in cells (e.g. a lone U+0301
      intab) survive the round trip through the `csv` module and through
      Frictionless?
- [ ] What shape does the error report take? We need to turn it into one
      readable REPL message per violation (resource, row, field, note).

**Gate:** if inline validation is impossible or too slow, fall back to
plain-Python checks driven by the same `datapackage.yaml`. That keeps the
format and the declarative schema while dropping the runtime dependency.

### Phase 1: Model fixes (no Frictionless yet)

These are needed whatever happens with persistence, and they make the
relational mapping clean.

- [ ] Remove `Token.id`. Build the pynini `SymbolTable` from token order with
      keys starting at **1**. Today the first token gets id `0`, which pynini
      treats as the epsilon label, so that token disappears from compiled
      FSAs.
- [ ] `TokenGroup.members: tuple[str, ...]` (token names, not ids).
- [ ] Use the same identifier field on Token, TokenGroup and Pattern (`name`).
      `acceptor._token_regex` and `get_token_group_map` currently read
      `.string` on TokenGroup and Pattern, and `_array_ids_unique` does the
      same with `field="string"`. Those classes only define `name`.
- [ ] `update_struct` replaces in place, keeping the struct's position. It no
      longer does remove + append.
- [ ] Tests: the first token compiles to a non-epsilon arc. Removing a token
      that a group uses fails loudly rather than re-pointing the group.
      Updating a struct keeps its order.

### Phase 2: `kayino/store.py`: the conversion layer between the registry and table rows

- [ ] Ship the schema as package data: `src/kayino/schema/datapackage.yaml`.
- [ ] `registry_to_tables(registry) -> dict[str, list[dict]]` and
      `tables_to_registry(tables) -> Registry`. Use `cattrs` unstructure /
      structure for each table. The `NormStep` union is split and joined on
      `kind` (the `cattrs.strategies.configure_tagged_union` strategy, or an
      explicit dispatch).
- [ ] `validate(registry) -> list[Violation]`: build the Package from
      `registry_to_tables`, run Frictionless, then the custom checks, and
      return the violations normalized into one shape.
- [ ] Tests (`tests/store_test.py`, parametrized from a jsonl file like the
      existing tests):
  - round trip: `tables_to_registry(registry_to_tables(r)) == r`
  - one case per constraint: a registry that violates it yields exactly the
    expected violation

### Phase 3: Replace the hooks in `structs.py`

- [ ] `register_struct`, `remove_struct` and `update_struct` become plain
      tuple operations followed by `validate(new_registry)`. Any violation
      raises `RegistryValidationError(violations)` (a `KayinoError`).
- [ ] Delete: `_struct_validation_hooks`, `_registry_validation_hooks`,
      `_field_hooks` (except the Text id helper), `_construction_hooks` and
      `_rename_field` (use a cattrs `override(rename=...)` instead),
      `_struct_mutation_hooks` (replace with generic tuple append/remove),
      `_pre_remove_hooks`, `_post_remove_hooks`, `_reindex_tokens`,
      `_check_depending_structs` and `_parse_members`.
- [ ] Delete the error classes nothing uses any more
      (`MalformedStructArrayError`, `StructDependencyError`).
- [ ] REPL: format `RegistryValidationError` as one line per violation.

### Phase 4: Persistence

- [ ] `store.dump(registry, path)` writes the CSVs and copies
      `datapackage.yaml`. `store.load(path)` reads them, validates, and
      returns the registry. Write CSVs as UTF-8 **with BOM**, so that Excel
      opens them correctly, or document the encoding. Phase 0 decides.
- [ ] REPL commands: `init DIR`, `save [DIR]`, `load DIR`. `ReplState` gains
      `project_dir: Path | None`.
- [ ] Remove the `.cache` mkdir that runs when `repl/app.py` is imported.
- [ ] Prebuilt norms are seeded as `norm_steps` rows on `init`, so the FKs
      from `spec_steps` resolve. `load` checks them against
      `PREBUILT_NORM_FUNCS`.
- [ ] Test: `frictionless validate my-language/datapackage.yaml` passes on
      a dumped project, i.e. the folder is valid without kayino installed.

### Phase 5: Docs

- [ ] Update `CLAUDE.md`, which still describes the old `repl.py` and spec
      model.
- [ ] README: project folder layout, and how to hand-edit the files and
      re-validate.

## Open decisions

1. **One `symbols` table vs. separate `tokens` / `groups` / `patterns`
   tables.** One table makes cross-kind name uniqueness a primary key but
   needs the custom kind check. Separate tables are friendlier to read but
   need a custom uniqueness check. Recommendation: one table.
2. **Persist `NormalizedText`?** It can be derived from texts + spec.
   Recommendation: don't persist it, or write it to a separate `outputs/`
   folder outside the data package.
3. **Validate on every command or only on save:** decided by the Phase 0
   timings.
4. **CLDF interop:** out of scope here. Worth an importer later, since users
   may already hold wordlists in CLDF.

## Risks

- Spreadsheet editors can silently change text: normalizing Unicode, trimming
  whitespace, or turning values like `1-2` into dates. Mitigation:
  `kayino load` always validates, and the README documents this hazard.
- Frictionless's maintenance pace. Mitigation: the Phase 0 gate fallback
  keeps the format independent of the library.
