# Hook refactor: spike learnings and a lighter direction

## Why this note

Two refactor spikes explored a table-oriented model: one declared schemas and
used Frictionless for project files, and another put the registry in SQLite.
Both produced useful fixes and clarified the data model. They also add layers
that Kayino does not currently need. The next pass should keep the existing
in-memory registry and hook-driven lifecycle, but make those hooks smaller,
more explicit, and more consistent.

This is a design note, not a claim that either spike is ready to merge. The
spike changes were explored in working branches; this summary records their
useful results and a possible next step.

## What the spikes taught us

### Useful model changes

- A symbol's public identity is its **name**, regardless of whether it is a
  token or a reusable pattern. Calling this field `string` on tokens and
  `name` on other symbol-like values caused real bugs and awkward generic
  code. Use one consistent name field.
- Token IDs do not need to be stored or kept contiguous. Pynini reserves label
  zero for epsilon, so derive symbol-table labels when compiling and start at
  one. This removes global reindexing whenever a token is added or removed.
- `TokenGroup` duplicates the idea of a pattern whose alternatives are token
  names. Represent the needed behavior through `Token` and `Pattern`, and
  remove the separate group struct if no remaining feature requires it.
- Mappings, ordered spec steps, and licit patterns are collections owned by a
  parent object. The row-shaped spike made ownership and deletion behavior
  clear, but a general table API is not required to express that relationship.
  Small domain helpers over the existing registry can keep the data model
  compact.
- Text IDs are a different case from token labels: they identify stored text
  records and should be stable when other texts are removed. Allocate them
  monotonically (or use a suitably unique ID), rather than deriving them from
  the current collection length.

### Bugs and behavior clarified during the work

- Token, group, and pattern uniqueness code read `.string` on types that only
  had `.name`; that prevented adding groups and patterns. A common name field
  and one uniqueness check remove this mismatch.
- The acceptor had the same field mismatch, and tokenization did not recognize
  pattern names. The spike fixes exercised consistent symbol lookup.
- Pynini label zero is epsilon. Assigning the first token label zero silently
  gave the first token the wrong semantics; derived labels must start at one.
- `byte_star` only accepted the empty string, so cdrewrite steps returned an
  empty result. It needs the closure of `BYTE`. Plain, insertion, contextual,
  and multibyte rules were checked in the spike.
- An empty cdrewrite input is valid for insertion rules and must not be
  rejected as an invalid required field.
- Mapping rewrites run in one regex pass, longest input first. They do not
  chain within the same mapping step; existing mapping cases pin that behavior.
- Existing REPL rough edges surfaced while checking behavior: `add rwr` can
  fail because the grammar lacks the required name slot, and quoted pattern
  values retain quote characters. These are separate command/parser fixes.

### Costs that argue against the libraries

- Frictionless validation had roughly 90 ms fixed overhead, versus about
  1–2 ms for the in-memory checks. Running it per command would make the REPL
  feel sluggish. Restricting it to file boundaries still leaves a second
  validation model and generated descriptor to maintain.
- The project format is small and Kayino already reads/writes its files.
  Frictionless adds descriptor generation, dependency and error translation
  code without being needed for the current workflow.
- SQLite makes constraints declarative, but brings connection lifecycle,
  generated DDL, transaction/error translation, mutable database semantics,
  and an API resembling a database layer. That is a substantial interface for
  a small in-process state model.
- Both approaches encourage generic schema machinery (table metadata,
  references, converters, and general selection/query helpers). Kayino's
  lifecycle rules are few enough to express directly and locally.

## Proposed light refactor

Keep the current registry representation and REPL-facing behavior. Replace
the sprawling hook registries with a small set of explicit domain operations
and a few table-like immutable structs where they make ownership clearer.
“Table-like” here means a regular collection of typed values with focused
helpers; it does not imply a database or generic table API.

### Data model

- Standardize symbol identity on `name`.
- Keep `Token` for atomic surfaces and `Pattern` for reusable expressions.
  Remove `TokenGroup`; express alternatives/grouping with pattern syntax or a
  pattern value, whichever fits existing semantics with the smallest model.
- Derive Pynini labels from the current token ordering, starting at 1. Make
  this conversion a single helper so compilation and decoding share it.
- Use stable, monotonically allocated IDs for persisted text records. Keep
  allocation in one helper and make the next-ID rule explicit, including what
  happens after deletion and project reload.
- Keep ordered collections explicit. If moving spec steps or mapping entries
  to row-like structs simplifies mutation, do so only for the affected
  collections; do not flatten every nested value preemptively.

### Lifecycle and checks

- Keep construction, update, rename, and removal as named domain operations.
  Each should produce a candidate registry, run the relevant checks, and
  return the candidate only on success. A refused REPL command must leave the
  old registry unchanged.
- Replace generic hook maps with direct checks grouped by purpose:
  - local construction checks (for example, compile a `Pattern` against the
    candidate symbols);
  - registry checks (unique symbol names, valid references, valid norm names);
  - collection helpers for append/remove/reorder and ID allocation.
- Use one shared symbol-name check across tokens and patterns. Check
  references to tokens explicitly where a feature requires a token rather
  than any symbol.
- Preserve referential behavior intentionally: owned values are removed with
  their parent; external references prevent deletion or are updated by an
  explicit rename operation. Avoid encoding this in a general-purpose
  foreign-key framework.
- Keep a clear distinction between **rename** (changes identity and updates
  references) and **replace** (changes data while preserving identity).

### Persistence and boundaries

- Keep the current simple project-file format unless a concrete user need
  requires another format. Put parsing, serialization, and validation at the
  file boundary; keep command-time validation in ordinary Python.
- Do not generate a database schema or a second package descriptor just to
  describe Kayino's own structs. A small explicit loader can validate required
  fields and then invoke the same domain checks used by the REPL.
- Preserve useful spike guarantees in focused tests: IDs start at one for FST
  labels; text IDs remain stable; names are unique; invalid references refuse
  the mutation; failed mutations preserve state; and cdrewrite insertion and
  contextual rules produce the intended output.

## Suggested sequence

1. Keep the bug fixes that are independent of the architecture: label numbering,
   `byte_star`, and corrected name-based symbol lookup.
2. Decide the smallest `Pattern` representation that replaces `TokenGroup`;
   migrate callers and tests, then remove the struct and its hooks.
3. Unify symbol names and token/pattern lookup behind a small set of helpers.
4. Make text ID allocation stable and centralized; cover gaps and reloads.
5. Simplify hook registries into direct lifecycle/check functions while
   preserving atomic refused mutations.
6. Only introduce table-like structs for collections whose mutation or
   ownership is currently error-prone. Stop when these operations read
   locally and the existing behavior is covered.

## Open design points

- Does `Pattern` already express every useful token-group behavior? Check
  callers and expected syntax before deleting `TokenGroup`.
- Should text IDs be integers allocated from `max(existing) + 1`, or opaque
  strings? The requirement is stability, not a particular representation.
- Which mutations can change pattern tokenization? Adding or renaming a token
  may change how an existing expression is segmented even when it still
  compiles. Decide whether to reject such changes, warn, or require explicit
  pattern edits.
- The token regex currently needs symbol names escaped. Punctuation in names
  can break or alter tokenization. Add a focused fix and cases as part of the
  name cleanup.
- Brackets in text or rewrite strings can raise Pynini string-compilation
  errors. Determine whether Kayino should escape them or report a domain error
  rather than terminating the REPL.
- `decode_labels` has call sites passing options it does not accept. Resolve
  this while consolidating the symbol-table conversion path.

## Stop condition

The refactor is done when the hook machinery is smaller and understandable,
IDs have explicit semantics, tokens and patterns share consistent naming,
`TokenGroup` is gone without losing required behavior, and rejected mutations
remain atomic. No database abstraction or generic schema framework is needed
to meet those goals.
