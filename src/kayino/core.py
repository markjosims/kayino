import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache

import pynini
from attrs import frozen
from pynini.lib.rewrite import lattice_to_one_top_string

from kayino.acceptor import (
    byte_star,
    filter_strings_by_pattern,
    fsa,
    get_sigma_star,
    tokenize_str,
)
from kayino.models import (
    CdrewriteArgs,
    FsaCompilationError,
    NormalizationSpec,
    NormalizationStatus,
    NormStep,
    Pattern,
    Registry,
)
from kayino.prebuilt import PREBUILT_NORM_FUNCS
from kayino.structs import fetch_struct


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


def _compile_cdrewrite(
    cdrewrite_args: CdrewriteArgs,
    registry: Registry | None,
) -> pynini.Fst:
    input_fsa = fsa(
        text=cdrewrite_args.input_str,
        registry=registry,
    )
    output_fsa = fsa(
        text=cdrewrite_args.output_str,
        registry=registry,
    )
    left_context = fsa(
        text=cdrewrite_args.left_context,
        registry=registry,
    )
    right_context = fsa(
        text=cdrewrite_args.right_context,
        registry=registry,
    )

    rewrite_fst = pynini.cdrewrite(
        tau=pynini.cross(input_fsa, output_fsa),
        l=left_context,
        r=right_context,
        sigma_star=byte_star(),
    )
    rewrite_fst.optimize()
    return rewrite_fst


def apply_cdrewrite(
    text: str,
    cdrewrite_args: CdrewriteArgs,
    registry: Registry | None = None,
) -> str:
    rewrite_fst = _compile_cdrewrite(
        cdrewrite_args,
        registry=registry,
    )
    result = lattice_to_one_top_string(text @ rewrite_fst)
    return result


def test_fits_patterns(
    text: str,
    patterns: tuple[str, ...] | str,
    registry: Registry,
) -> tuple[str, ...] | None:

    if type(patterns) is str:
        patterns = (patterns,)

    try:
        text_fsa = fsa(text=text, registry=registry)
    except Exception as e:
        raise FsaCompilationError(f"Failed to compile text {text}") from e

    valid_patterns = []
    for pattern_name in patterns:
        pattern = fetch_struct(
            struct_type_name=Pattern.__name__,
            field="string",
            value=pattern_name,
            registry=registry,
        )
        pattern_fsa = fsa(text=pattern.value, registry=registry)
        intersection = filter_strings_by_pattern(
            input_fsa=text_fsa,
            pattern_fsa=pattern_fsa,
            registry=registry,
        )
        if intersection:
            valid_patterns.append(pattern_name)

    return tuple(valid_patterns)


def intersect_pattern(
    target: str,
    pattern: str,
    registry: Registry,
) -> tuple[str, ...]:
    try:
        pattern_fsa = fsa(text=pattern, registry=registry)
    except Exception as e:
        raise FsaCompilationError(f"Failed to compile pattern {pattern}") from e

    try:
        target_fsa = fsa(text=target, registry=registry)
    except Exception as e:
        raise FsaCompilationError(f"Failed to compile target {target}") from e

    intersection = filter_strings_by_pattern(
        input_fsa=target_fsa,
        pattern_fsa=pattern_fsa,
        registry=registry,
    )
    return tuple(intersection)


@lru_cache(maxsize=16384)
def apply_step(
    text: str,
    step: NormStep,
    registry: Registry | None = None,
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
            registry=registry,
        )
    elif step.kind == "mapping":
        return apply_mappings(
            text=text,
            mappings=step.data,
        )
    raise ValueError("Unknown normstep type", step.kind)


@lru_cache(maxsize=16384)
def normalize(
    text: str,
    spec: NormalizationSpec,
    registry: Registry,
) -> tuple[str, NormalizationStatus, tuple[str, ...] | None]:
    result = text
    for step_name in spec.steps:
        step = fetch_struct(
            struct_type_name=NormStep.__name__,
            field="name",
            value=step_name,
            registry=registry,
        )
        result = apply_step(
            text=result,
            step=step,
            registry=registry,
        )
    status: NormalizationStatus = "licit"
    status_payload = None

    tokens = tokenize_str(
        text=result,
        registry=registry,
        no_patterns_or_regex=True,
    )
    oov_tokens = tuple(token.string for token in tokens if token.kind == "oov")
    if oov_tokens:
        status = "illicit_token"
        status_payload = oov_tokens
        return result, status, status_payload
    valid_patterns = test_fits_patterns(
        text=result,
        patterns=spec.licit_patterns,
        registry=registry,
    )
    if not valid_patterns:
        status = "illicit_pattern"
    else:
        status_payload = tuple(valid_patterns)
    return result, status, status_payload


@frozen
class CorpusReport:
    oov_occurrences: int
    distinct_oov_characters: int
    licit: int
    illicit_token: int
    compilation_error: int
    illicit_pattern: int


def summarize_corpus(
    texts: Iterable[str],
    spec: NormalizationSpec,
    registry: Registry,
) -> CorpusReport:
    outcomes: Counter[NormalizationStatus] = Counter()
    oov_occurrences = 0
    oov_characters: set[str] = set()

    for text in texts:
        try:
            _, status, payload = normalize(text, spec, registry)
        except FsaCompilationError:
            status = "compilation_error"
        else:
            if status == "illicit_token" and payload:
                oov_occurrences += len(payload)
                oov_characters.update(payload)
        outcomes[status] += 1

    return CorpusReport(
        oov_occurrences=oov_occurrences,
        distinct_oov_characters=len(oov_characters),
        licit=outcomes["licit"],
        illicit_token=outcomes["illicit_token"],
        compilation_error=outcomes["compilation_error"],
        illicit_pattern=outcomes["illicit_pattern"],
    )
