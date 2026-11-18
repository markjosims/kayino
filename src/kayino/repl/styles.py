"""
## Text formatting styles
"""

_white = "#ffffff"
_green = "#33aa33"
_red = "#ff0000"
_blue = "#0000ff"
_purple = "#800080"
_black = "#ffffff"
_maroon = "#662222"
_yellow = "#FFBF00"


TOKEN_STYLES = {
    "operator": f"{_blue} bold",
    "token": f"{_purple} bold",
    "unrecognized": f"bg:{_maroon} {_black}",
    "separator": _white,
    "group": _yellow,
}
INPUT_STYLES = {
    "verb": f"{_green} bold",
    "arg": _white,
}
