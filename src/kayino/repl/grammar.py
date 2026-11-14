"""
## Command parsing grammar
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from enum import Enum

from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar, compile

from kayino.models import (
    NAME_TO_STRUCT,
    CdrewriteNorm,
    MappingNorm,
    Pattern,
    Token,
    TokenGroup,
)

"""
## Struct shorthands
"""

_struct_name_to_shorthands = defaultdict(list)

for struct_name in NAME_TO_STRUCT.keys():
    _struct_name_to_shorthands[struct_name].append(struct_name.lower())

_struct_name_to_shorthands[Token.__name__].append("tok")
_struct_name_to_shorthands[TokenGroup.__name__].append("tkg")
_struct_name_to_shorthands[Pattern.__name__].append("pat")
_struct_name_to_shorthands[MappingNorm.__name__].append("map")
_struct_name_to_shorthands[CdrewriteNorm.__name__].append("rwr")

_shorthand_to_struct_name = {
    s: k for k, v in _struct_name_to_shorthands.items() for s in v
}


"""
## Regex helpers
"""


def _regex_union(words: Sequence[str]) -> str:
    return "|".join(words)


"""
## Regex string over all struct types
"""

_all_struct_names = []
for struct_type_name, shorthands in _struct_name_to_shorthands.items():
    _all_struct_names.extend(shorthands + [struct_type_name])
_all_struct_regex = _regex_union(_all_struct_names)

"""
## Arg capture groups
"""

_word_or_quoted = r"(\"[^\"]+\"|\w+)"


class ArgStrings(str, Enum):
    # single word
    STRING = r"(?P<string>\w+)"
    # list of whitespace-separated words, with an optional final word
    # lacking trailing whitespace
    # words may optionally be wrapped in quotation marks
    STRING_LIST = rf"(?P<string_list>({_word_or_quoted}\s+)*({_word_or_quoted})?)"
    # single word
    VALUE = r"(?P<value>\w+)"
    # single word with angle brackets
    PATTERN_STRING = r"(?P<string><\w+>)"
    # digit sequence
    INDEX = r"(?P<id>\d+)"
    # whitespace-delimited digit sequence
    INDEX_LIST = r"(?P<idlist>[\d\s]+)"
    # single word
    NAME = r"(?P<name>\w+)"
    # freeform text (including empty string)
    DESCRIPTION = r"(?P<description>.*)"
    # sequence of word > word pairs separated by semicolons
    # the last of which does not need the semicolon
    MAPPING = rf"(?P<mapping>({_word_or_quoted}\s+>\s+{_word_or_quoted};)*\s+({_word_or_quoted}\s+>\s+{_word_or_quoted})?)"
    # CdrewriteNorm inputs/outputs are all single word or quoted strings
    INPUT_STR = rf"(?P<input_str>{_word_or_quoted})"
    OUTPUT_STR = rf"(?P<output_str>{_word_or_quoted})"
    # Context strs have an L: and R: prefix.
    LEFT_CONTEXT = rf"L:(?P<left_context>{_word_or_quoted})"
    RIGHT_CONTEXT = rf"R:(?P<right_context>{_word_or_quoted})"


def _parse_string_list(text: str) -> list[str]:
    parts = re.findall(_word_or_quoted, text)
    parts = [part.replace('"', "") for part in parts]
    return parts


def _parse_mapping(text: str) -> list[tuple[str, str]]:
    breakpoint()
    return re.findall(
        _word_or_quoted + r"\s+>\s+" + _word_or_quoted + ";?",
        text,
    )


_argstring_parse_hooks: dict[str, callable] = {
    "string_list": _parse_string_list,
    "index_list": re.compile(r"\b(\d+)\b").findall,
    "mapping": _parse_mapping,
    "left_context": lambda s: s.removeprefix("L:"),
    "right_context": lambda s: s.removeprefix("R:"),
}


_struct_builder_patterns = {
    Token.__name__: ArgStrings.STRING,
    Pattern.__name__: f"({ArgStrings.PATTERN_STRING} \\s+ {ArgStrings.VALUE})",
    TokenGroup.__name__: f"({ArgStrings.STRING} \\s+ {ArgStrings.INDEX_LIST}|{ArgStrings.STRING_LIST})",
    MappingNorm.__name__: rf"{ArgStrings.NAME} \s+ {ArgStrings.MAPPING} \s+ {ArgStrings.DESCRIPTION}",
    CdrewriteNorm.__name__: rf"({ArgStrings.INPUT_STR} \s+ {ArgStrings.OUTPUT_STR} \s+ {ArgStrings.LEFT_CONTEXT}? \s+ {ArgStrings.RIGHT_CONTEXT}?)",
}

_struct_builder_arg_template = (
    r"((?P<verb>add) \s+ (?P<struct>{struct}) \s+ {struct_args})"
)
_add_struct_patterns = []

for struct_type_name, pattern in _struct_builder_patterns.items():
    shorthand_list = _regex_union(
        _struct_name_to_shorthands[struct_type_name] + [struct_type_name]
    )
    _add_struct_patterns.append(
        _struct_builder_arg_template.format(
            struct=shorthand_list,
            struct_args=pattern,
        )
    )

_add_struct_str = _regex_union(_add_struct_patterns)

_list_struct_str = r"((?P<verb>l(ist)?) \s+ (?P<struct>{struct}))".format(
    struct=_all_struct_regex
)

_struct_builder_arg_template = (
    r"((?P<verb>add) \s+ (?P<struct>{struct}) \s+ {struct_args})"
)


_struct_removal_patterns = {
    Token.__name__: "foo",
    Pattern.__name__: r"(?P<string>\w+)",
    TokenGroup.__name__: r"(?P<string>\w+)",
    MappingNorm.__name__: r"(?P<name>\w+)",
    CdrewriteNorm.__name__: r"(?P<index>\d)",
}

for struct_type_name, pattern in _struct_removal_patterns.items():
    shorthand_list = _regex_union(
        _struct_name_to_shorthands[struct_type_name] + [struct_type_name]
    )
    _add_struct_patterns.append(
        _struct_builder_arg_template.format(
            struct=shorthand_list,
            struct_args=pattern,
        )
    )


def create_grammar() -> _CompiledGrammar:
    return compile(
        _regex_union(
            (
                _add_struct_str,
                _list_struct_str,
                _remove_struct_str,
            )
        )
    )
