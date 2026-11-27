# FSA compilation in Kayino

[The acceptor module](../../src/kayino/acceptor.py) compiles Kayino expressions to Pynini acceptors. The registry supplies named patterns and inventory tokens. The symbol table determines how FST labels represent those tokens. These are separate choices.

| Call | Label representation | Registry behavior |
| --- | --- | --- |
| `fsa(expression)` | UTF-8 bytes | Unregistered characters are allowed. References such as `<Stem>` are rejected. |
| `fsa(expression, registry)` | UTF-8 bytes | Named patterns can expand and unregistered characters are allowed. |
| `fsa(expression, registry, use_symbol_table=True)` | Symbol-table IDs | Registered tokens can occupy one label even when they contain multiple characters. Out-of-inventory literals fail compilation. |

## Tokenization and parsing

- Quantifiers apply to the preceding atom. With no matching inventory token, `aŋ+` accepts `aŋŋ` and rejects `aŋaŋ`. A registered multicharacter token can be one atom, so `ch+` with registered `ch` repeats the whole token. Byte labels still encode that token's UTF-8 bytes.
- Pattern references use angle brackets. Definitions can depend on other named patterns, and dependencies are resolved in topological order. Direct and mutual recursion raise `graphlib.CycleError`. Cache tests exercise both label modes and reuse a pattern name with different registry contents.
- Preprocessing trims leading and trailing whitespace and collapses internal whitespace runs to one space before tokenization. Compare a whitespace-heavy expression with its canonical spelling when testing. An expression that produces no tokens compiles to epsilon, including whitespace-only input.
- Regex tests should check accepted and rejected strings. A successful call can still compile an operator as literal text. For repetition, compose with finite candidate acceptors to test membership. Malformed input such as `(a` or `a|` should raise `ValueError` instead of leaking a parser indexing error.
- Negated classes need a defined alphabet. The current implementation subtracts from the registry inventory, so the ability to compile unregistered literals does not imply Unicode-wide complements.

## Decoding and contextual rewrites

High-level compilation and decoding helpers select the mode with `use_symbol_table`. Lower-level parser helpers and `decode_labels` receive the actual table or `None`. Use the same label representation when compiling, composing, and decoding.

In byte mode, `ŋ` has two labels, `0xC5` and `0x8B`. Calling `chr()` on each yields two different characters. The decoder skips epsilon labels, collects the remaining bytes, and decodes the buffer as UTF-8. With a symbol table, it resolves each nonzero label through that table. Decoder tests also check input and output projection, weights, and shortest-path limits.

[Contextual rewrites](../../src/kayino/core.py) compile the match, replacement, and both contexts through the byte-mode acceptor. Their labels must agree with `byte_star` and the input text. For example, a rule mapping `x` to `y` with left context `(é|ŋ)` can match `éx` and `ŋx` before inventory validation.

## Regression tests

Run `uv run pytest -q tests/acceptor_test.py`. The cases live in [compilation](../../tests/test_cases/acceptor_compilation_tests.jsonl), [regex](../../tests/test_cases/acceptor_regex_tests.jsonl), and [decoding](../../tests/test_cases/acceptor_decoding_tests.jsonl) JSONL files.
