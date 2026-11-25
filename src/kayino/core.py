import re
from collections.abc import Mapping, Sequence
from os import wait

import pynini
from pynini.lib.byte import BYTE
from pynini.lib.rewrite import lattice_to_one_top_string

from kayino.models import (
    CdrewriteNorm,
    NormalizationSpec,
    NormalizationStatus,
    NormStep,
    Registry,
)
from kayino.prebuilt import PREBUILT_NORM_FUNCS
from kayino.structs import fetch, select

byte_star = pynini.closure(BYTE)


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
    rule: CdrewriteNorm,
) -> str:
    rewrite_fst = pynini.cdrewrite(
        tau=pynini.cross(rule.input_str, rule.output_str),
        l=rule.left_context,
        r=rule.right_context,
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
    registry: Registry,
) -> str:
    if step.kind == "prebuilt":
        return apply_prebuilt(
            text=text,
            norm_id=step.name,
        )
    if step.kind == "cdrewrite":
        return apply_cdrewrite(
            text=text,
            rule=step,
        )
    elif step.kind == "mapping":
        return apply_mappings(
            text=text,
            mappings={
                row.intab: row.outtab for row in select(registry, "MappingRow", norm=step.name)
            },
        )
    raise ValueError("Unknown normstep type", step.kind)


def normalize(
    text: str,
    spec: NormalizationSpec,
    registry: Registry,
) -> tuple[str, NormalizationStatus, tuple[str, ...] | None]:
    for spec_step in select(registry, "SpecStep", spec=spec.name):
        step = fetch(registry, NormStep.__name__, name=spec_step.step)
        text = apply_step(
            text=text,
            step=step,
            registry=registry,
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
