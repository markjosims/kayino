from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, TypeAliasType, TypedDict, get_args

from attrs import Factory, fields, frozen
from frozendict import frozendict

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
    string: str
    kind: Literal["token"] = "token"


@frozen
class AnonToken:
    string: str
    kind: Literal["unrecognized", "operator"]


@frozen
class Pattern:
    string: str
    value: str
    kind: Literal["pattern"] = "pattern"


type Symbol = Token | Pattern
type TokenLike = Token | AnonToken | Pattern

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
    data: frozendict[str, str] = Factory(frozendict)
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
    steps: tuple[str, ...] = Factory(tuple)
    licit_patterns: tuple[str, ...] = Factory(tuple)


# Status indicating whether a string conforms to the constraints
# defined by a normalization spec
NormalizationStatus = Literal[
    "licit",
    "illicit_token",
    "illicit_pattern",
    "compilation_error",
]

"""
### Text

String data, serves as inputs and outputs of Kayino normalizers.
"""

TextSource = Literal["user", "file"]


@frozen
class Text:
    """
    A raw (unnormalized) string with metadata about provenance.
    Currently does not store a unique ID, instead other data
    and commands can reference a text by its index in the array
    of registered texts.
    """

    string: str
    source_type: TextSource = "user"
    source_value: str = Factory(lambda: str(datetime.now()))


@frozen
class NormalizedText:
    """
    The output of some normalization spec and a status value.
    `text_id` is a positional index of the source text, if
    applicable.
    """

    string: str
    spec_name: str
    status: NormalizationStatus
    text_id: int | None = None


"""
## Name->type mapping for all structs
"""

NAME_TO_STRUCT = {
    struct_type.__name__: struct_type
    for struct_type in [
        Token,
        Pattern,
        Symbol,
        NormStep,
        PrebuiltNorm,
        MappingNorm,
        CdrewriteNorm,
        NormalizationSpec,
        Text,
        NormalizedText,
    ]
}
NAME_TO_STRUCT = frozendict(NAME_TO_STRUCT)


TYPE_TO_UNION = {}
for union_type in (Symbol, NormStep):
    for subtype in get_args(union_type.__value__):
        TYPE_TO_UNION[subtype.__name__] = union_type.__name__
TYPE_TO_UNION = frozendict(TYPE_TO_UNION)

MACRO_STRUCT_TYPES = tuple(
    name for name in NAME_TO_STRUCT.keys() if name not in TYPE_TO_UNION
)


def resolve_struct_type_name(struct_type_name: str) -> str:
    """
    If `struct_type_name` is the member of a union struct,
    return the macro name for the union. Otherwise, return
    `struct_type_name`.

    This function is useful as the registry stores union
    member structs under the macro struct name.
    """
    macro_type = TYPE_TO_UNION.get(
        struct_type_name,
        struct_type_name,
    )
    return macro_type


def resolve_struct_type_fields(struct_type_name: str) -> list[str]:
    struct_type = NAME_TO_STRUCT[struct_type_name]
    if isinstance(struct_type, TypeAliasType):
        struct_type = struct_type.__value__
        subtypes = get_args(struct_type)
        type_fields = set(field.name for member in subtypes for field in fields(member))
        type_fields = sorted(type_fields)
        return type_fields
    return sorted(field.name for field in fields(struct_type))


"""
## RegistryType

All structs for a given Kayino state are stored in a registry,
which is implemented as a dictionary mapping the name of a struct
type to an array of structs.
"""


@frozen
class Registry:
    Pattern: tuple[Pattern, ...] = tuple()
    Symbol: tuple[Symbol, ...] = tuple()
    NormStep: tuple[NormStep, ...] = tuple()
    NormalizationSpec: tuple[NormalizationSpec, ...] = tuple()


ID_FIELDS = ("id", "name")

"""
## Registry helpers
"""


def get_structs(
    struct_type_name: str,
    registry: Registry,
) -> tuple[object, ...]:
    if union_type := TYPE_TO_UNION.get(struct_type_name, None):
        union_struct_array = getattr(registry, union_type)
        filtered = tuple(
            filter(
                (lambda struct: type(struct).__name__ == struct_type_name),
                union_struct_array,
            )
        )
        return filtered
    return getattr(registry, struct_type_name)


"""
## Regex symbols

Enumeration over regex symbols for purposes of compiling FSAs
from pattern strings.
"""


class RegexSymbols(str, Enum):
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


class ReservedSymbols(str, Enum):
    SIGMA = "<Sigma>"
    EPSILON = "<Empty>"


"""
## Special error classes
"""


class KayinoError(Exception):
    pass


class DuplicateStructError(KayinoError):
    pass


class StructNotFoundError(KayinoError):
    pass


class MalformedStructArrayError(KayinoError):
    pass


class FsaCompilationError(KayinoError):
    pass


class StructDependencyError(KayinoError):
    pass


class StructRenameError(KayinoError):
    pass


class MappingConflictError(KayinoError):
    pass


class StateSaveError(KayinoError):
    pass


class StateLoadError(KayinoError):
    pass
