"""
# prebuilt.py

Module registering basic normalization functions for use in a
normalization set.
"""

import functools
import re
import unicodedata
from collections.abc import Callable
from string import punctuation

from kayino.constants import EMPTY_SYMBOL
from kayino.models import PrebuiltNorm

PREBUILT_NORMS: dict[str, PrebuiltNorm] = {}
PREBUILT_NORM_FUNCS: dict[str, Callable[[str], str]] = {}

for norm in ("NFC", "NFKC", "NFD", "NFKD"):
    norm_name = norm + "_normalization"
    PREBUILT_NORMS[norm_name] = PrebuiltNorm(
        name=norm_name,
        description=f"{norm} unicode normalization",
    )
    PREBUILT_NORM_FUNCS[norm_name] = functools.partial(unicodedata.normalize, norm)

PREBUILT_NORMS["lowercase"] = PrebuiltNorm(
    name="lowercase", description="Convert text to lowercase."
)
PREBUILT_NORM_FUNCS["lowercase"] = str.lower
PREBUILT_NORMS["remove_punct"] = PrebuiltNorm(
    name="remove_punct",
    description="Remove all punctuation",
)
_punct_re = re.compile("[" + re.escape(punctuation) + "]")
PREBUILT_NORM_FUNCS["remove_punct"] = functools.partial(_punct_re.sub, "")


"""
## Universal preprocessing

Unicode point replacement and whitepsace normalization happen across
ALL text inputs in Kayino, and are not considered a toggleable normalization
step.
"""

_unicode_token = re.compile(r"U\+([a-f0-9]{4})", flags=re.IGNORECASE)


def _point_to_char(hex_str: re.Match) -> str:
    return chr(int(hex_str.group(1), 16))


def _replace_unicode(text: str):
    """
    For now not a public normalization step, but rather automatically applied
    to all text inputs (so that e.g. a Token can be written with a unicode point.
    """
    return _unicode_token.sub(_point_to_char, text)


def _normalize_whitespace(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _remove_empty_symbol(text: str) -> str:
    text = text.replace(EMPTY_SYMBOL, "")
    return text


def universal_preproc(text: str) -> str:
    text = _replace_unicode(text)
    text = _remove_empty_symbol(text)
    text = _normalize_whitespace(text)
    return text
