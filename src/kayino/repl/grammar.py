"""
## Command parsing grammar
"""

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from enum import Enum

from prompt_toolkit.contrib.regular_languages.compiler import _CompiledGrammar, compile

from kayino.models import (
    NAME_TO_STRUCT,
    CdrewriteNorm,
    MappingConflictError,
    MappingNorm,
    NormalizationSpec,
    NormStep,
    Pattern,
    Text,
    Token,
)

"""
## Struct shorthands
"""

_struct_name_to_shorthands = defaultdict(list)

for struct_name in NAME_TO_STRUCT.keys():
    _struct_name_to_shorthands[struct_name].append(struct_name.lower())

_struct_name_to_shorthands[Token.__name__].append("tok")
_struct_name_to_shorthands[Pattern.__name__].append("pat")
_struct_name_to_shorthands[MappingNorm.__name__].append("map")
_struct_name_to_shorthands[CdrewriteNorm.__name__].append("rwr")
_struct_name_to_shorthands[CdrewriteNorm.__name__].append("rule")
_struct_name_to_shorthands[NormalizationSpec.__name__].append("normspec")
_struct_name_to_shorthands[NormalizationSpec.__name__].append("spec")
_struct_name_to_shorthands[NormStep.__name__].append("step")
_struct_name_to_shorthands[NormStep.__name__].append("stp")

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
    # angle-bracket wrapped word
    PATTERN_WORD = r"(?P<{arg_name}><\w+>)"
    # digit sequence
    INDEX = r"(?P<{arg_name}>\d+)"
    # freeform text (including empty string)
    TEXT = r"(?P<{arg_name}>.*)"
    # quoted text (to prevent collision with other args)
    QUOTED = r'"(?P<{arg_name}>[^\"]*)"'
    # sequence of word > word pairs separated by semicolons
    # the last of which does not need the semicolon
    MAPPING = (
        rf"(?P<{{arg_name}}>({_word_or_quoted}\s+>\s+{_word_or_quoted}\s*;\s+)*"
        rf"({_word_or_quoted}\s+>\s+{_word_or_quoted};?)?)"
    )
    # Context strs have an L: and R: prefix.
    LEFT_CONTEXT = rf"L:(?P<{{arg_name}}>{_word_or_quoted})"
    RIGHT_CONTEXT = rf"R:(?P<{{arg_name}}>{_word_or_quoted})"

    REWRITE = (
        rf"(?P<{{arg_name}}>{_word_or_quoted}\s+>\s+{_word_or_quoted}"
        rf"(\s+L:{_word_or_quoted})?(\s+R:{_word_or_quoted})?)"
    )


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
_string_text = ArgStrings.TEXT.value.format(arg_name="string")

_quoted_text = ArgStrings.QUOTED.value.format(arg_name="text")
_description = ArgStrings.QUOTED.value.format(arg_name="description")

_left_context = ArgStrings.LEFT_CONTEXT.value.format(arg_name="left_context")
_right_context = ArgStrings.RIGHT_CONTEXT.value.format(arg_name="right_context")

_index = ArgStrings.INDEX.value.format(arg_name="index")

_mapping = ArgStrings.MAPPING.value.format(arg_name="mapping")

_rwr_args = (
    rf"(?P<rwr_args>{_input_string}\s+>\s+{_output_string}"
    rf"(\s+{_left_context})?(\s+{_right_context})?)"
)


_struct_builder_patterns = {
    Token.__name__: _string_word,
    Pattern.__name__: rf"{_pattern_string} \s+ {_value_string}",
    MappingNorm.__name__: rf"{_name_word} \s+ {_mapping} (\s+ {_description})?",
    CdrewriteNorm.__name__: rf"{_name_word} \s+ {_rwr_args}",
    Text.__name__: _string_text,
    NormalizationSpec.__name__: _name_word,
}

_struct_removal_patterns = {
    Token.__name__: rf"({_string_word}|{_index})",
    Pattern.__name__: rf"({_pattern_string}|{_index})",
    MappingNorm.__name__: rf"({_name_word}|{_index})",
    CdrewriteNorm.__name__: rf"({_name_word}|{_index})",
    Text.__name__: _index,
    NormalizationSpec.__name__: _name_word,
}


"""
## Argword-specific parsing hooks

_argstring_parse_hooks: Hooks that are called on the
parsed value of a given argword, e.g. to parse a
string of mappings a > b; b > c into a dictionary
{"a": "b", "b": "c"}.

_argstring_rename_map: A mapping of argword to
struct field names. Needed because argword->struct field
is a many-to-one mapping, e.g. multiple argwords are
parsed as the `data` field for a struct.
"""


def _parse_mapping(text: str) -> dict[str, str]:
    pairs = re.findall(
        _word_or_quoted + r"\s+>\s+" + _word_or_quoted + ";?",
        text,
    )
    intabs = [pair[0] for pair in pairs]
    intab_counter = Counter(intabs)
    duplicate = [pair for pair in pairs if intab_counter[pair[0]] > 1]
    if duplicate:
        raise MappingConflictError(f"Duplicate intabs: {duplicate}")
    return dict(pairs)


def _parse_rewrite_args(text: str) -> dict:
    match = re.match(_rwr_args, text)
    kwargs = match.groupdict()
    kwargs.pop("rwr_args")
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    return kwargs


_argstring_parse_hooks: dict[str, Callable] = {
    "mapping": _parse_mapping,
    "rwr_args": _parse_rewrite_args,
    "index": int,
    "string": lambda text: text.replace('"', "") if text is not None else "",
    "value": lambda text: text.replace('"', "") if text is not None else "",
    "description": lambda text: text.replace('"', "") if text is not None else "",
}
_argstring_rename_map: dict[str, str] = {
    "mapping": "data",
    "rwr_args": "data",
}
_argstrings_to_drop: tuple[str, ...] = (
    "left_context",
    "right_context",
    "input_str",
    "output_str",
)


"""
### Simple commands

REPL commands that do not need specific args per struct type
"""

_simple_command_templates = {
    "read": rf"((?P<verb>read) \s+ {_text})",
    "tokenize": rf"((?P<verb>tokenize|tkz) \s+ ({_index}|{_quoted_text}|{_text}))",
    "process": rf"((?P<verb>process|pro?c) \s+ ({_index}|{_quoted_text}|{_text}))",
    "apply": rf"((?P<verb>apply|apl) \s+ {_name_word} \s+ ({_index}|{_quoted_text}|{_text}))",
    "oov": rf"((?P<verb>oov) (\s+(?P<oov_type>text|token))? (\s+(?P<return_type>first|all))?)",
    "load": rf"((?P<verb>load) \s+(?P<path>.*))",
    "save": rf"((?P<verb>save) \s+(?P<path>.*))",
    "nonascii": rf"((?P<verb>nonascii) \s+(?P<target>raw|processed))",
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
    r"((?P<verb>spec) \s+ (?P<subverb>app(end)?|p(op)?)"
    r"\s+ (?P<field>{field}) \s+ {field_args})"
)
_modify_spec_field_patterns = {
    "steps": _name_word,
    "licit_patterns": _pattern_string,
}
_modify_spec_field2shorthand = {
    "steps": ["ste?p"],
    "licit_patterns": ["pat(tern)?s?", "lp"],
}
_modify_spec_shorthand2field = {
    "step": "steps",
    "stp": "steps",
    "patterns": "licit_patterns",
    "pat": "licit_patterns",
    "pats": "licit_patterns",
    "lp": "licit_patterns",
}
_activate_spec_template = (
    rf"((?P<verb>spec) \s+ (?P<subverb>act(ivate)?) (\s+ {_name_word})?)"
)


def spec_command_str() -> str:
    command_strs = []
    for k, v in _modify_spec_field_patterns.items():
        field_str = _regex_union(_modify_spec_field2shorthand.get(k, []) + [k])
        command_strs.append(
            _modify_spec_template.format(
                **{
                    "field": field_str,
                    "field_args": v,
                }
            )
        )
    command_strs.append(_activate_spec_template)
    return _regex_union(command_strs)


"""
### Special commands

List aggregating all special command strs
(so far only NormalizationSpec, more to come!)
"""

_special_command_strs = []
_special_command_strs.append(spec_command_str())

"""
### Shorthands
"""

_shorthand_to_command_name = {
    "tkz": "tokenize",
    "prc": "process",
    "proc": "process",
    "rm": "remove",
    "p": "pop",
    "app": "append",
    "apl": "apply",
    "l": "list",
    "a": "add",
    "act": "activate",
}

_shorthand_maps = {
    "verb": _shorthand_to_command_name,
    "subverb": _shorthand_to_command_name,
    "field": _modify_spec_shorthand2field,
}


def populate_struct_command_template(
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
        command_str = populate_struct_command_template(
            command_template=template,
            format_key="struct_args",
            patterns=patterns,
        )
        command_strs.append(command_str)
    command_strs.extend(_special_command_strs)

    grammar_str = _regex_union(command_strs)
    return compile(grammar_str)


def _normalize_user_response(text: str) -> str:
    """
    Normalization functions that apply to all user inputs
    """
    return text.strip()


def parse_command(
    user_response: str, grammar: _CompiledGrammar
) -> dict[str, str] | None:
    user_response = _normalize_user_response(user_response)
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
    for key, shorthand_map in _shorthand_maps.items():
        if value := match_kwargs.get(key, None):
            match_kwargs[key] = shorthand_map.get(value, value)
    for argword, struct_field in _argstring_rename_map.items():
        if value := match_kwargs.pop(argword, None):
            match_kwargs[struct_field] = value
    match_kwargs = {
        k: v for k, v in match_kwargs.items() if k not in _argstrings_to_drop
    }
    return match_kwargs


if __name__ == "__main__":
    g = create_grammar()
    test_str = "add tok foo"
    result = parse_command(test_str, g)
    breakpoint()
