import re
from collections.abc import Mapping, Sequence
from os import wait

import pynini
from pynini.lib.rewrite import lattice_to_one_top_string

from kayino.models import (
    CdrewriteArgs,
    CdrewriteNorm,
    MappingNorm,
    NormalizationSpec,
    NormalizationStatus,
    NormStep,
    PrebuiltNorm,
    Registry,
)
from kayino.prebuilt import PREBUILT_NORM_FUNCS, PREBUILT_NORMS
from kayino.structs import fetch_struct

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
    result = lattice_to_one_top_string(text @ rewrite_fst)
    return result


def test_fits_patterns(
    text: str,
    patterns: Mapping[str, str],
) -> tuple[str, ...] | None:
    # TODO: use pynini fsas instead of regexes
    valid_patterns = []
    for key, pattern in patterns.items():
        if re.fullmatch(pattern, text):
            valid_patterns.append(key)
    return tuple(valid_patterns) or None


def apply_step(
    text: str,
    step: NormStep,
) -> str:
    if step.kind == "prebuilt":
        return apply_prebuilt(
            text=text,
            norm_id=step.name,
        )
    if step.kind == "cdrewrite":
        return apply_cdrewrite(
            text=text,
            cdrewrite_args=step.data,
        )
    elif step.kind == "mapping":
        return apply_mappings(
            text=text,
            mappings=step.data,
        )
    raise ValueError("Unknown normstep type", step.kind)


def normalize(
    text: str,
    spec: NormalizationSpec,
    registry: Registry,
) -> tuple[str, NormalizationStatus, tuple[str, ...] | None]:
    for step_name in spec.steps:
        step = fetch_struct(
            struct_type_name=NormStep.__name__,
            field="name",
            value=step_name,
            registry=registry,
        )
        text = apply_step(
            text=text,
            step=step,
        )
    status: NormalizationStatus = "licit"
    status_payload = None
    # TODO: status resolution uses updated logic
    # if status_payload := find_illicit_tokens(text, spec.token_set):
    # status = "illicit_token"
    # elif status_payload := test_fits_patterns(text, spec.patterns):
    # pass
    # else:
    # status = "illicit_pattern"
    return text, status, status_payload
