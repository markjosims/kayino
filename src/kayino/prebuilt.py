"""
# prebuilt.py

Module registering basic normalization functions for use in a
normalization set.
"""

import functools
import re
import unicodedata
from string import punctuation
from typing import Callable

from kayino.models import PrebuiltNorm

PREBUILT_NORMS: dict[str, PrebuiltNorm] = {}
PREBUILT_NORM_FUNCS: dict[str, Callable[[str], str]] = {}

for norm in ("NFC", "NFKC", "NFD", "NFKD"):
    norm_name = norm + "_normalization"
    PREBUILT_NORMS[norm_name] = PrebuiltNorm(
        data=norm_name,
        name=norm_name,
        description=f"{norm} unicode normalization",
    )
    PREBUILT_NORM_FUNCS[norm_name] = functools.partial(unicodedata.normalize, norm)

PREBUILT_NORMS["lowercase"] = PrebuiltNorm(
    data="lowercase", name="lowercase", description="Convert text to lowercase."
)
PREBUILT_NORM_FUNCS["lowercase"] = str.lower
PREBUILT_NORMS["remove_punct"] = PrebuiltNorm(
    data="remove_punct",
    name="remove_punct",
    description="Remove all punctuation",
)
PREBUILT_NORM_FUNCS["remove_punct"] = lambda text: re.sub(
    re.escape(punctuation), "", text
)
