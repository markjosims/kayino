"""
## Text formatting styles
"""

from enum import Enum

_white = "#ffffff"
_green = "#33aa33"
_red = "#ff0000"
_orange = "#ffa500"
_blue = "#0000ff"
_purple = "#800080"
_black = "#ffffff"
_maroon = "#662222"
_yellow = "#FFBF00"


class TOKEN_STYLES(str, Enum):
    operator = f"{_blue} bold"
    token = f"{_purple} bold"
    oov = f"bg:{_maroon} {_black}"
    separator = _white


class STATUS_STYLES(str, Enum):
    success = f"{_green} bold"


class INPUT_STYLES(str, Enum):
    verb = f"{_green} bold"
    subverb = f"{_blue} bold"
    struct = f"{_red}"
    field = f"{_orange}"
    input_str = f"{_orange}"
    output_str = f"{_orange}"
    left_context = f"{_orange}"
    right_context = f"{_orange}"
    name = f"{_blue} italic"
    index = f"{_yellow}"
    arg = _white
