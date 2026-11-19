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
    WORD = r"(?P<{arg_name}>\w+)"
    # word or quoted string
    STRING = rf"(?P<{{arg_name}}>{_word_or_quoted})"
    # list of whitespace-separated words, with an optional final word
    # lacking trailing whitespace
    # words may optionally be wrapped in quotation marks
    STRING_LIST = rf"(?P<{{arg_name}}>({_word_or_quoted}\s+)*({_word_or_quoted})?)"
    # angle-bracket wrapped word
    PATTERN_WORD = r"(?P<{arg_name}><\w+>)"
    # digit sequence
    INDEX = r"(?P<{arg_name}>\d+)"
    # whitespace-delimited digit sequence
    INDEX_LIST = r"(?P<{arg_name}>[\d\s]+)"
    # freeform text (including empty string)
    TEXT = r"(?P<{arg_name}>.*)"
    # sequence of word > word pairs separated by semicolons
    # the last of which does not need the semicolon
    MAPPING = rf"(?P<{{arg_name}}>({_word_or_quoted}\s+>\s+{_word_or_quoted}\s*;)*\s+({_word_or_quoted}\s+>\s+{_word_or_quoted})?)"
    # Context strs have an L: and R: prefix.
    LEFT_CONTEXT = rf"L:(?P<{{arg_name}}>{_word_or_quoted})"
    RIGHT_CONTEXT = rf"R:(?P<{{arg_name}}>{_word_or_quoted})"


# ArgString + arg name combos
_string_word = ArgStrings.WORD.value.format(arg_name="string")
_name_word = ArgStrings.WORD.value.format(arg_name="name")
_kind_word = ArgStrings.WORD.value.format(arg_name="kind")

_pattern_string = ArgStrings.PATTERN_WORD.value.format(arg_name="string")

# you see there are "strings" and then there are "string strings"
_string_string = ArgStrings.STRING.value.format(arg_name="string")
_value_string = ArgStrings.STRING.value.format(arg_name="value")
_input_string = ArgStrings.STRING.value.format(arg_name="input_str")
_output_string = ArgStrings.STRING.value.format(arg_name="output_str")

_text = ArgStrings.TEXT.value.format(arg_name="text")
_string_text = ArgStrings.TEXT.value.format(arg_name="text")
_description = ArgStrings.TEXT.value.format(arg_name="description")

_left_context = ArgStrings.LEFT_CONTEXT.value.format(arg_name="left_context")
_right_context = ArgStrings.RIGHT_CONTEXT.value.format(arg_name="right_context")

_index = ArgStrings.INDEX.value.format(arg_name="index")

_string_list = ArgStrings.STRING_LIST.value.format(arg_name="string_list")
_index_list = ArgStrings.INDEX_LIST.value.format(arg_name="string_list")

_mapping = ArgStrings.MAPPING.value.format(arg_name="mapping")

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
    Token.__name__: _string_word,
    Pattern.__name__: f"({_pattern_string} \\s+ {_value_string})",
    TokenGroup.__name__: f"({_pattern_string} \\s+ ({_index_list}|{_string_list}))",
    MappingNorm.__name__: rf"{_name_word} \s+ {_mapping} \s+ {_description}",
    CdrewriteNorm.__name__: rf"({_input_string} \s+ {_output_string} "
    rf"\s+ {_left_context}? \s+ {_right_context}?)",
    Text.__name__: _string_text,
    NormalizationSpec.__name__: _name_word,
}

_struct_removal_patterns = {
    Token.__name__: rf"({_string_word}|{_index})",
    Pattern.__name__: rf"({_pattern_string}|{_index})",
    TokenGroup.__name__: rf"({_pattern_string}|{_index})",
    MappingNorm.__name__: rf"({_name_word}|{_index})",
    CdrewriteNorm.__name__: rf"({_name_word}|{_index})",
    Text.__name__: _index,
    NormalizationSpec.__name__: _name_word,
}

"""
### Simple commands

REPL commands that do not need specific args per struct type
"""

_simple_command_templates = {
    "read": rf"((?P<verb>read) \s+ {_text})",
    "tokenize": rf"((?P<verb>tokenize|tkz) \s+ (?P<string>{_text}))",
}

"""
### Struct commands

REPL commands with a slot for specific args by struct type
"""

_struct_command_templates = {
    "add": r"((?P<verb>add|a) \s+ (?P<struct>{struct}) \s+ {struct_args})",
    "remove": r"((?P<verb>remove|rm) \s+ (?P<struct>{struct}) \s+ {struct_args})",
    "list": r"((?P<verb>l(ist)?) \s+ (?P<struct>{struct}))",
}
_struct_command_patterns = {
    "add": _struct_builder_patterns,
    "remove": _struct_removal_patterns,
}

"""
### NormalizationSpec commands

Special REPL commands specific to the NormalizationSpec struct
"""

_modify_spec_template = (
    r"((?P<verb>spec) \s+ (?P<subverb>add|a|remove|rm) \s+ {field_args})"
)
_modify_spec_field_patterns = {
    "steps": _name_word,
    "licit_patterns": _text,
}

_activate_spec_template = (
    rf"((?P<verb>spec) \s+ (?P<subverb>act(ivate)?) (\s+ {_name_word})?)"
)

"""
### Special commands

Dictionary aggregating all special command strs
(so far only NormalizationSpec, more to come!)
"""

_special_command_templates = {
    "spec": _regex_union(
        (
            _modify_spec_template,
            _activate_spec_template,
        )
    ),
}
_special_command_patterns = {
    "spec": _modify_spec_field_patterns,
}


_shorthand_to_command_name = {
    "tkz": "tokenize",
    "rm": "remove",
    "l": "list",
    "a": "add",
    "act": "activate",
}


def format_command_regex(
    command_template: str,
    format_key: str,
    patterns: dict[str, str] | None = None,
) -> str:
    """
    Generate a single regex string that defines the parsing logic for
    some REPL command.

    Args:
      command_template: A string with format slots {struct} and optionally
                        {struct_args}.
      patterns:         An optional dictionary mapping struct types to regex
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
        if patterns:
            pattern = patterns.get(struct_type_name, "")
            format_kwargs[format_key] = pattern
        command_patterns.append(command_template.format(**format_kwargs))

    command_str = _regex_union(command_patterns)
    return command_str


def create_grammar() -> _CompiledGrammar:
    command_strs = []
    for command, template in _simple_command_templates.items():
        command_strs.append(template)
    for command, template in _struct_command_templates.items():
        patterns = _struct_command_patterns.get(command, None)
        command_str = format_command_regex(
            command_template=template,
            format_key="struct_args",
            patterns=patterns,
        )
        command_strs.append(command_str)
    for command, template in _special_command_templates.items():
        if patterns := _special_command_patterns.get(command, None):
            command_str = format_command_regex(
                command_template=template,
                format_key="field_args",
                patterns=patterns,
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

    if subverb := match_kwargs.pop("subverb", None):
        match_kwargs["subverb"] = _shorthand_to_command_name.get(
            subverb,
            subverb,
        )
    return match_kwargs


if __name__ == "__main__":
    g = create_grammar()
    test_str = "add tok foo"
    result = parse_command(test_str, g)
    breakpoint()
