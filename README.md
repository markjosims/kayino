# Kayino

Kayino is an interactive REPL for building a text normalization pipeline for
a low-resource language, and for checking normalized text against an
inventory of tokens and patterns.

A project is a folder of plain CSV files that you can read, diff in git, and
edit in a spreadsheet.

## Install and run

Kayino uses [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync          # install dependencies
uv run kayino    # start the REPL
uv run pytest    # run the tests
```

## Concepts

| Thing | What it is |
|---|---|
| **token** | A unit of the writing system, e.g. a letter or digraph (`a`, `ch`). |
| **token group** | A named set of tokens, e.g. `<V>` for vowels. Names are in angle brackets. |
| **pattern** | A named regex-like expression over tokens, e.g. `<P>` = `ab`. |
| **norm step** | One normalization step: a *prebuilt* function (`lowercase`, `NFC_normalization`, ...), a *mapping* of intab → outtab strings, or a *cdrewrite* contextual rewrite rule. |
| **spec** | A normalization pipeline: an ordered list of norm steps, plus the patterns that define well-formed output. |
| **text** | An input string to normalize. |

Tokens, groups and patterns share one namespace: a name can only be used once
across all three.

Mappings are applied in one pass, longest intab first, so rewrites do not
chain: with `a > b; b > c`, `ab` becomes `bc`, not `cc`.

## REPL commands

Struct names have shorthands: `tok` (token), `tkg` (token group), `pat`
(pattern), `map` (mapping norm), `rwr`/`rule` (cdrewrite norm), `step`/`stp`
(any norm step), `spec`, `text`.

### Building a project

```
add tok a                      # a / add: add a token
add tkg <V> a e i              # group with members a, e, i (in order)
add pat <P> ab                 # pattern
add map m a > b; ch > c        # mapping norm "m"
add text Hello there           # a text
read path/to/file.txt          # add every line of a file as a text
add spec s                     # a spec
spec act s                     # make s the active spec
spec add step lowercase        # append a step to the active spec
spec rm step lowercase         # remove every occurrence of a step
```

### Inspecting and changing

```
list tok                       # l / list: table of tokens (or tkg, map, spec, step, text, ...)
remove tok a                   # rm / remove
rename tok a c                 # mv / rename (see below)
tokenize "abc"                 # tkz: split a string into tokens (or a text index: tkz 0)
process "ABC"                  # proc: normalize with the active spec (or a text index)
```

Every change is checked against the project's constraints. A change that would
break one is refused, and the session is left as it was. For example:

```
> remove tok a
RegistryCheckError: registry check failed:
  group_members ('<V>', 0).token: 'a' not found in symbols.name
```

Removing a group, mapping or spec also removes what belongs to it (its
members, mapping pairs, or steps and patterns). Removing something that other
things still use (a token in a group, a norm step in a spec) is refused.

**Renaming** updates everything that refers to the renamed thing: group
members, spec steps, mapping rows, and pattern strings that mention a renamed
token. A rename is refused if it would change how an existing pattern splits
into tokens. For example, renaming `x` to `ab` while a pattern `ab` means
`a` followed by `b` is refused, because `ab` would then read as the single new
token. Tokens, groups, patterns, norm steps and specs can be renamed.

### Project folders

```
init my-language               # create a project folder from the current session
save                           # save to the session's project folder
save other-folder              # save somewhere else (becomes the project folder)
load my-language               # replace the session with a project folder
validate                       # check the project folder on disk
export s outputs/normalized.csv   # normalize all texts with spec s, write CSV
```

`init` and `save` refuse to write into a folder that has other files in it
and is not a kayino project. `load` replaces the current session **without
asking**: save first. `export` writes outside the project data and refuses to
overwrite the project's own files.

## The project folder

```
my-language/
  datapackage.yaml     # GENERATED: describes the CSV files (do not edit)
  symbols.csv          # name, kind (token|group|pattern), value (pattern only)
  group_members.csv    # group, position, token
  norm_steps.csv       # name, description, kind (prebuilt|mapping|cdrewrite),
                       #   input_str, output_str, left_context, right_context
  mapping_rows.csv     # norm, intab, outtab
  specs.csv            # name, description
  spec_steps.csv       # spec, position, step
  spec_patterns.csv    # spec, pattern
  texts.csv            # string, id, source_type (user|file), source_value
```

`datapackage.yaml` is a [Frictionless Data Package](https://datapackage.org/)
descriptor, so other tools can validate the folder too, e.g.
`frictionless validate my-language/datapackage.yaml`. Kayino rewrites it on
every save and never reads it back: it always validates against the schema
built into the code.

### Editing the CSV files by hand

All CSV files are safe to edit. After editing, run `validate` in the REPL (or
just `load`, which refuses an invalid folder and lists every problem).

- Files are UTF-8, with `\n` line endings and no byte order mark. Some
  spreadsheet programs need to be told the encoding when opening them.
- **Order** of group members and spec steps comes from the `position` column,
  not from row order. To reorder, edit the numbers. Gaps are fine; kayino
  renumbers on save.
- **Empty cells**: an empty `outtab` means "delete the intab"; empty
  descriptions, contexts and pattern values are allowed. Names, intabs,
  references, kinds and numbers must not be empty.
- Watch out for spreadsheet programs silently changing text: normalizing
  Unicode (which can merge a letter and a combining accent), trimming spaces,
  or turning values like `1-2` into dates.

## Known issues

- Status checking after normalization (tokens and licit patterns) is not
  implemented yet: `process` and `export` always report `licit`.
- cdrewrite rules can't be added from the REPL (`add rwr ...` crashes the
  REPL: the grammar has no slot for the rule's name).
- Texts or rewrite rules containing `[` or `]` crash `process` and `export`
  when a cdrewrite step runs (pynini reads brackets as symbol syntax; the
  strings need `pynini.escape`).
- `spec add pat` / `spec rm pat` crash the REPL (the grammar and handler
  disagree on the argument name).
- Pattern values given in quotes keep the quote characters, so they don't
  compile.
- Patterns that use token groups or other patterns can be added to the
  inventory but don't compile yet.
- Adding a token can silently change how an existing pattern tokenizes
  (renames are guarded; plain additions are not).
- An unexpected (non-kayino) error ends the REPL session.
