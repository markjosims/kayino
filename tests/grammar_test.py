"""
Test the REPL grammar against sample arg strings.
"""

import json
import re
from pathlib import Path

import attrs
import pytest

from kayino.repl.grammar import (
    ArgStrings,
    _argstring_parse_hooks,
    create_grammar,
    parse_command,
)

ARGWORD_FILE = Path(__file__).parent / "test_cases" / "argword_tests.jsonl"
ARGWORDS = [json.loads(line) for line in ARGWORD_FILE.read_bytes().splitlines()]

COMMAND_FILE = Path(__file__).parent / "test_cases" / "command_tests.jsonl"
COMMANDS = [json.loads(line) for line in COMMAND_FILE.read_bytes().splitlines()]


@pytest.mark.parametrize("argword_test", ARGWORDS)
def test_argwords(argword_test):
    arg_word = argword_test["arg_word"]
    arg_key = argword_test["arg_key"]
    user_input = argword_test["user_input"]
    expected_value = argword_test["expected_value"]
    arg_pattern = ArgStrings[arg_word].format(arg_name=arg_key)
    result = re.match(
        arg_pattern,
        user_input,
    )
    if expected_value is None:
        assert result is None
    else:
        arg_value = result.groupdict()[arg_key]
        if arg_key in _argstring_parse_hooks:
            arg_value = _argstring_parse_hooks[arg_key](arg_value)
        if attrs.has(arg_value):
            arg_value = attrs.asdict(arg_value)
        assert arg_value == expected_value


@pytest.mark.parametrize("command_test", COMMANDS)
def test_commands(command_test):
    grammar = create_grammar()
    user_input = command_test["user_input"]
    expected_kwargs = command_test["expected_kwargs"]
    parsed = parse_command(user_input, grammar)
    for k, v in parsed.items():
        if attrs.has(v):
            parsed[k] = attrs.asdict(v)

    assert expected_kwargs == parsed
