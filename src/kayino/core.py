import re
from typing import Mapping, Sequence

import pynini

from kayino.models import (
    CdrewriteArgs,
    CdrewriteNorm,
    MappingNorm,
    NormalizationSpec,
    NormalizationStatus,
    NormStep,
    PrebuiltNorm,
)
from kayino.prebuilt import PREBUILT_NORM_FUNCS, PREBUILT_NORMS

byte_star = pynini.closure(pynini.string_map(""))


def sorted_intab_regex(intab_array: Sequence[str]) -> re.Pattern:
    pattern = re.compile(
        "|".join(
            re.escape(intab)
            for intab in sorted(
                intab_array,
                key=len,
                reverse=True,
            )
        )
    )
    return pattern


def apply_mappings(
    text: str,
    mappings: Mapping[str, str],
) -> str:
    if not mappings:
        return text
    pattern = sorted_intab_regex(mappings.keys())
    text = pattern.sub(lambda match: mappings[match.group(0)], text)
    return text


def apply_prebuilt(
    text: str,
    norm_id: str,
) -> str:
    norm_func = PREBUILT_NORM_FUNCS[norm_id]
    text = norm_func(text)
    return text


def apply_cdrewrite(
    text: str,
    cdrewrite_args: CdrewriteArgs,
) -> str:
    rewrite_fst = pynini.cdrewrite(
        tau=pynini.cross(cdrewrite_args.input_str, cdrewrite_args.output_str),
        l=cdrewrite_args.left_context,
        r=cdrewrite_args.right_context,
        sigma_star=byte_star,
    )


_licit_flag = "[licit:]"


def find_illicit_tokens(
    text: str,
    token_set: Mapping[str, str],
) -> tuple[str, ...] | None:
    if not token_set:
        return False
    token_regex = sorted_intab_regex(token_set.values())
    licit_marked = token_regex.sub(lambda _: _licit_flag, text)
    remaining_tokens = licit_marked.split(_licit_flag)
    remaining_tokens = filter(bool, remaining_tokens)
    remaining_tokens = tuple(remaining_tokens)
    return remaining_tokens or None


def test_fits_patterns(
    text: str,
    patterns: Mapping[str, str],
) -> tuple[str, ...] | None:
    valid_patterns = []
    for key, pattern in patterns.items():
        if re.fullmatch(pattern, text):
            valid_patterns.append(key)
    return tuple(valid_patterns) or None


def normalize(
    text: str,
    spec: NormalizationSpec,
) -> tuple[str, NormalizationStatus, tuple[str, ...] | None]:
    text = apply_prebuilt(text, spec.steps)
    text = apply_mappings(text, spec.mappings)
    status: NormalizationStatus = "licit"
    status_payload = None
    if status_payload := find_illicit_tokens(text, spec.token_set):
        status = "illicit_token"
    elif status_payload := test_fits_patterns(text, spec.patterns):
        pass
    else:
        status = "illicit_pattern"
    return text, status, status_payload
