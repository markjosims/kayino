"""
Test the REPL grammar against sample arg strings.
"""

import json
import re
from pathlib import Path

import pytest

from kayino.repl.grammar import ArgStrings, _argstring_parse_hooks

ARGWORD_FILE = Path(__file__).parent / "argword_tests.jsonl"
ARGWORDS = [json.loads(line) for line in ARGWORD_FILE.read_bytes().splitlines()]


@pytest.mark.parametrize("argword_test", ARGWORDS)
def test_argwords(argword_test):
    arg_word = argword_test["arg_word"]
    arg_key = argword_test["arg_key"]
    user_input = argword_test["user_input"]
    expected_value = argword_test["expected_value"]
    result = re.match(ArgStrings[arg_word], user_input)
    if expected_value is None:
        assert result is None
    else:
        arg_value = result.groupdict()[arg_key]
        if arg_key in _argstring_parse_hooks:
            arg_value = _argstring_parse_hooks[arg_key](arg_value)
        assert arg_value == expected_value
