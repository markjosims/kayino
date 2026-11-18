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
    NormalizationSpec,
    Pattern,
    Text,
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
    INDEX_LIST = r"(?P<id_list>[\d\s]+)"
    # single word
    NAME = r"(?P<name>\w+)"
    # freeform text (including empty string)
    DESCRIPTION = r"(?P<description>.*)"
    TEXT = r"(?P<text>.*)"
    # sequence of word > word pairs separated by semicolons
    # the last of which does not need the semicolon
    MAPPING = rf"(?P<mapping>({_word_or_quoted}\s+>\s+{_word_or_quoted}\s*;)*\s+({_word_or_quoted}\s+>\s+{_word_or_quoted})?)"
    # CdrewriteNorm inputs/outputs are all single word or quoted strings
    INPUT_STR = rf"(?P<input_str>{_word_or_quoted})"
    OUTPUT_STR = rf"(?P<output_str>{_word_or_quoted})"
    # Context strs have an L: and R: prefix.
    LEFT_CONTEXT = rf"L:(?P<left_context>{_word_or_quoted})"
    RIGHT_CONTEXT = rf"R:(?P<right_context>{_word_or_quoted})"


"""
## Argword-specific parsing hooks
"""


def _parse_string_list(text: str) -> list[str]:
    parts = re.findall(_word_or_quoted, text)
    parts = [part.replace('"', "") for part in parts]
    return parts


def _parse_mapping(text: str) -> list[tuple[str, str]]:
    return re.findall(
        _word_or_quoted + r"\s+>\s+" + _word_or_quoted + ";?",
        text,
    )


def _parse_id_list(text: str) -> list[int]:
    return list(map(int, re.findall(r"\b(\d+)\b", text)))


_argstring_parse_hooks: dict[str, callable] = {
    "string_list": _parse_string_list,
    "id_list": _parse_id_list,
    "mapping": _parse_mapping,
    "left_context": lambda s: s.removeprefix("L:"),
    "right_context": lambda s: s.removeprefix("R:"),
}


_struct_builder_patterns = {
    Token.__name__: ArgStrings.STRING.value,
    Pattern.__name__: f"({ArgStrings.PATTERN_STRING.value} \\s+ {ArgStrings.VALUE.value})",
    TokenGroup.__name__: f"({ArgStrings.STRING.value} \\s+ ({ArgStrings.INDEX_LIST.value}|{ArgStrings.STRING_LIST.value}))",
    MappingNorm.__name__: rf"{ArgStrings.NAME.value} \s+ {ArgStrings.MAPPING.value} \s+ {ArgStrings.DESCRIPTION.value}",
    CdrewriteNorm.__name__: rf"({ArgStrings.INPUT_STR} \s+ {ArgStrings.OUTPUT_STR} "
    rf"\s+ {ArgStrings.LEFT_CONTEXT.value}? \s+ {ArgStrings.RIGHT_CONTEXT.value}?)",
    Text.__name__: ArgStrings.STRING.value,
    NormalizationSpec.__name__: ArgStrings.NAME.value,
}

_struct_removal_patterns = {
    Token.__name__: rf"({ArgStrings.STRING.value}|{ArgStrings.INDEX.value})",
    Pattern.__name__: rf"({ArgStrings.PATTERN_STRING.value}|{ArgStrings.INDEX.value})",
    TokenGroup.__name__: rf"({ArgStrings.PATTERN_STRING.value}|{ArgStrings.INDEX.value})",
    MappingNorm.__name__: rf"({ArgStrings.NAME.value}|{ArgStrings.INDEX.value})",
    CdrewriteNorm.__name__: rf"({ArgStrings.NAME.value}|{ArgStrings.INDEX.value})",
    Text.__name__: ArgStrings.INDEX.value,
    NormalizationSpec.__name__: ArgStrings.NAME.value,
}

_command_templates = {
    "add": r"((?P<verb>add) \s+ (?P<struct>{struct}) \s+ {struct_args})",
    "remove": r"((?P<verb>remove|rm) \s+ (?P<struct>{struct}) \s+ {struct_args})",
    "list": r"((?P<verb>l(ist)?) \s+ (?P<struct>{struct}))",
    "tokenize": rf"((?P<verb>tokenize|tkz) \s+ (?P<string>{ArgStrings.TEXT.value}))",
    "read": rf"((?P<verb>read) \s+ {ArgStrings.TEXT.value})",
}
_command_patterns = {
    "add": _struct_builder_patterns,
    "remove": _struct_removal_patterns,
}
_shorthand_to_command_name = {
    "tkz": "tokenize",
    "rm": "remove",
    "l": "list",
}


def get_struct_command_regex(
    command_template: str,
    struct_arg_patterns: dict[str, str] | None = None,
) -> str:
    """
    Generate a single regex string that defines the parsing logic for
    some REPL command.

    Args:
      command_template:     A string with format slots {struct} and optionally
                            {struct_args}.
      struct_arg_patterns:  An optional dictionary mapping struct types to regex
                            patterns for parsing the given command on that struct.
                            If None, then assumes no struct-specific args and does
                            not attempt to format the {struct_args} slot in the
                            template string.
    """
    command_patterns = []
    for struct_type_name in NAME_TO_STRUCT.keys():
        shorthand_list = _regex_union(
            _struct_name_to_shorthands[struct_type_name] + [struct_type_name]
        )
        format_kwargs = {"struct": shorthand_list}
        if struct_arg_patterns:
            pattern = struct_arg_patterns.get(struct_type_name, "")
            format_kwargs["struct_args"] = pattern
        command_patterns.append(command_template.format(**format_kwargs))

    command_str = _regex_union(command_patterns)
    return command_str


def create_grammar() -> _CompiledGrammar:
    command_strs = []
    for command, template in _command_templates.items():
        patterns = _command_patterns.get(command, None)
        if "struct" in template:
            command_str = get_struct_command_regex(
                command_template=template,
                struct_arg_patterns=patterns,
            )
        else:
            command_str = template

        command_strs.append(command_str)
    grammar_str = _regex_union(command_strs)
    return compile(grammar_str)


def parse_command(
    user_response: str, grammar: _CompiledGrammar
) -> dict[str, str] | None:
    match = grammar.match(user_response)
    if match is None:
        return
    match_kwargs = {kwarg.varname: kwarg.value for kwarg in match.variables()}
    match_kwargs = {
        k: _argstring_parse_hooks[k](v) if k in _argstring_parse_hooks else v
        for k, v in match_kwargs.items()
    }
    if "struct" in match_kwargs:
        struct_kwarg = match_kwargs.pop("struct")
        match_kwargs["struct"] = _shorthand_to_struct_name.get(
            struct_kwarg,
            struct_kwarg,
        )
    verb = match_kwargs.pop("verb")
    match_kwargs["verb"] = _shorthand_to_command_name.get(verb, verb)
    return match_kwargs


if __name__ == "__main__":
    g = create_grammar()
    test_str = "add tok foo"
    result = parse_command(test_str, g)
    breakpoint()
