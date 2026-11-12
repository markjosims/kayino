from __future__ import annotations

from enum import Enum
from typing import Literal

from attrs import Factory, frozen
from frozendict import frozendict

"""
## RegistryType

All structs for a given Kayino state are stored in a registry,
which is implemented as a dictionary mapping the name of a struct
type to an array of structs.
"""

RegistryType = dict[str, list[object, ...]]

"""
## Struct definitions

All data structs used in Kayino.

### Token and pattern structs

Structs for storing individual token strings (each corresponding
to a unique symbol in a pynini.SymbolTable), groups of tokens and
named patterns.
"""


@frozen
class Token:
    """
    A named token and its ID in the symbol table.
    """

    string: str
    id: int


@frozen
class AnonToken:
    string: str
    kind: Literal["unrecognized", "operator"]


@frozen
class TokenGroup:
    string: str
    members: tuple[int, ...]


@frozen
class Pattern:
    string: str
    value: str


type TokenLike = Token | AnonToken | TokenGroup | Pattern

"""
### Norm

A function that maps a string to a normalized output string.
The struct data itself must be JSON serializable, so each
type of norm function defines the data fields needed to interpret 
the function.
"""


@frozen
class PrebuiltNorm:
    """
    Pre-written functions defined in kayino/prebuilt.py.
    The struct only stores the function name and a human-friendly
    description.
    """

    name: str
    description: str = ""
    kind: Literal["prebuilt"] = "prebuilt"


@frozen
class MappingNorm:
    """
    A mapping of intab -> outtab strings.
    """

    name: str
    data: frozendict[str, str]
    description: str = ""
    kind: Literal["mapping"] = "mapping"


@frozen
class CdrewriteArgs:
    """
    Input arguments for pynini.cdrewrite.
    """

    input_str: str = ""
    output_str: str = ""
    left_context: str = ""
    right_context: str = ""


@frozen
class CdrewriteNorm:
    """
    A contextual rewrite rule.
    """

    name: str
    data: CdrewriteArgs
    description: str = ""
    kind: Literal["cdrewrite"] = "cdrewrite"


# Union of all norm types
type NormStep = PrebuiltNorm | MappingNorm | CdrewriteNorm


@frozen
class NormalizationSpec:
    """
    An ordered list of normalization functions defining a language's textnorm
    pipeline, and an array of patterns whose union defines the set of all
    valid strings for a given language.
    """

    name: str
    steps: tuple[NormStep, ...] = Factory(tuple)
    licit_patterns: tuple[Pattern, ...] = Factory(tuple)


# Status indicating whether a string conforms to the constraints
# defined by
NormalizationStatus = Literal["licit", "illicit_token", "illicit_pattern"]

"""
## Name->type mapping for all structs
"""

NAME_TO_STRUCT = {
    struct_type.__name__: struct_type
    for struct_type in [
        Token,
        TokenGroup,
        Pattern,
        NormStep,
        NormalizationSpec,
    ]
}

"""
## Regex symbols

Enumeration over regex symbols for purposes of compiling FSAs
from pattern strings.
"""


class RegexSymbols(Enum):
    STAR = "*"
    PLUS = "+"
    OPTIONAL = "?"
    UNION = "|"
    CARET = "^"
    LEFT_PAREN = "("
    RIGHT_PAREN = ")"
    LEFT_BRACKET = "["
    RIGHT_BRACKET = "]"
    DOT = "."


LEFT_DELIMITERS = (
    RegexSymbols.LEFT_BRACKET,
    RegexSymbols.LEFT_PAREN,
)
RIGHT_DELIMITERS = (
    RegexSymbols.RIGHT_BRACKET,
    RegexSymbols.RIGHT_PAREN,
)
UNARY_OPERATORS = (
    RegexSymbols.STAR,
    RegexSymbols.PLUS,
    RegexSymbols.OPTIONAL,
)
