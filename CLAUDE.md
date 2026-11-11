# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Your role: consultant, not developer

The user is the developer of this project. Claude acts only as a **software consultant**: give design critique, point out bugs, and suggest approaches when asked. Do not edit source or test files, add features, or refactor unless the user explicitly asks for that specific change. When suggesting code, show it in the reply rather than applying it.

## Project

Kayino is an interactive REPL for building a custom text normalization pipeline for a low-resource language. A pipeline is made of:
1. prebuilt normalization functions (`src/kayino/prebuilt.py`),
2. user-defined intab→outtab character/string mappings,
3. (planned) `pynini.cdrewrite` context-dependent rewrite rules.

After normalization, the output is checked against a whitelist of tokens and a set of regex patterns that define well-formed text.

## Commands

The project uses `uv` and Python 3.12 (`uv_build` backend).

```bash
uv sync                                   # install deps, including the dev group
uv run kayino                             # start the REPL (entry point: kayino:main)
uv run pytest                             # run all tests
uv run pytest tests/core_test.py::test_no_transitivity   # run a single test
uv run pytest -k "ki cho"                 # filter parametrized cases by id
```

No linter or formatter is configured.

## Architecture

**`core.py`: the pure pipeline.** `NormalizationSpec` is a frozen attrs class holding `mappings`, `patterns`, `token_set` (all `frozendict[str, str]`) and `prebuilt_norms` (a tuple of registry keys). `normalize(text, spec)` runs in a fixed order: prebuilt functions (in tuple order), then mappings, then validation. It returns `(text, status, payload)`, where status is `licit`, `illicit_token` or `illicit_pattern`.
- Mappings are applied in **one regex pass** with the longest key matched first (`sorted_intab_regex`). Rewrites therefore do not chain: output of one mapping is never re-matched by another. `tests/mapping_tests.jsonl` pins this down.
- Token validation replaces every licit token (the *values* of `token_set`) with a sentinel, splits on it, and reports whatever is left over as illicit.
- Pattern validation uses `re.match` (anchored at the start only) and returns the names of the patterns that matched.

**`prebuilt.py`: the registry.** `PREBUILT_REGISTRY: dict[str, PrebuiltNormalizer]` (a NamedTuple of `func`, `name`, `description`) is filled at import time. `NormalizationSpec.prebuilt_norms` refers to entries by their key.

**`repl.py`: the stateful shell.** `ReplState` (frozen attrs) holds *workspace* variables (`texts`, `patterns`, `token_set`, `mappings`) plus the *active* `spec: NormalizationSpec`. A name declared without the `spec` prefix goes to the workspace only. Only the `spec` fields affect `normalize`. All state changes return a new state through `attrs.evolve`.
- Command dispatch: `handle_user_response` tries each `validate_*` function in turn. Each one regex-matches the input and returns `(message, new_state)` on a match, or `None` otherwise. To add a command, write a `validate_*` function and add it to that list.
- Grammar: `[spec] (text|map|token|pattern) NAME = VALUE`, `print [spec] KIND[s]`, `norm[alize] TEXT|$textvar`, `save NAME`, `load NAME`.
- `save`/`load` serialize the whole `ReplState` to JSON with cattrs, in `./.cache/NAME`. The directory is relative to the working directory and is created when `repl.py` is imported.
- The REPL has no command yet for setting `spec.prebuilt_norms`.

## Tests

`tests/core_test.py` is parametrized from `tests/mapping_tests.jsonl`. Each line is `{"text", "expected", "mappings"}`. Add mapping cases by adding lines there.
