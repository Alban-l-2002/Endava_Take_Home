"""AI fallback for stage and geography normalisation (terminal-only, no DB writes)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from src.classification_rules import (
    GEOGRAPHY_AI_EXCLUDED_RAW_VALUES,
    GEOGRAPHY_WHITELIST_TO_STANDARD,
    STAGE_AI_CANONICAL_OPTIONS,
)
from src.validators import is_present, normalise_geography, normalise_stage

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

AiFallbackConfidence = Literal["High", "Medium", "none"]

_STAGE_CANONICAL_BY_LOWER = {
    stage.lower(): stage for stage in STAGE_AI_CANONICAL_OPTIONS
}


@dataclass
class AiFallbackOutcome:
    raw_value: str
    normalised_value: str | None
    confidence: AiFallbackConfidence
    ai_response: str
    note: str = ""


@dataclass
class AiFallbackFieldReport:
    field_name: str
    deterministic_none_total: int
    excluded_blank: int
    excluded_special_case: int
    eligible_for_ai: int
    unique_values_sent_to_ai: int
    corrected_medium: int
    corrected_high: int
    no_match: int
    api_errors: int
    outcomes: list[AiFallbackOutcome] = field(default_factory=list)


def _get_anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


def _normalise_lookup_key(raw_value: Any) -> str:
    return str(raw_value).strip().lower()


def is_geography_ai_fallback_eligible(raw_value: Any) -> bool:
    """Return True when geography should be sent to AI fallback."""
    if normalise_geography(raw_value) is not None:
        return False
    if not is_present(raw_value):
        return False
    return _normalise_lookup_key(raw_value) not in GEOGRAPHY_AI_EXCLUDED_RAW_VALUES


def is_stage_ai_fallback_eligible(raw_value: Any) -> bool:
    """Return True when stage should be sent to AI fallback."""
    if normalise_stage(raw_value) is not None:
        return False
    return is_present(raw_value)


def _build_geography_fallback_prompt(raw_value: str) -> str:
    return (
        "Is the following text a misspelling or formatting variant of a real country? "
        "If yes, return the correctly spelled country name. "
        "If it appears to be a different real country not relevant here, "
        "or is not a recognisable real place at all, return exactly: NO_MATCH.\n"
        f"Value: {raw_value}"
    )


def _build_stage_fallback_prompt(raw_value: str) -> str:
    stage_list = ", ".join(STAGE_AI_CANONICAL_OPTIONS)
    return (
        "Is the following text a misspelling or formatting variant of one of these "
        f"specific funding stages: {stage_list}? "
        "If yes, return the correct matching stage name exactly as listed. "
        "If it does not match any of these, return exactly: NO_MATCH.\n"
        f"Value: {raw_value}"
    )


def _extract_response_text(message: Any) -> str:
    response_text = ""
    for block in message.content:
        if block.type == "text":
            response_text += block.text
    return response_text.strip()


def _call_claude(prompt: str, *, client: Any, model: str) -> str:
    message = client.messages.create(
        model=model,
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_response_text(message)


def _parse_no_match_response(response_text: str) -> str | None:
    cleaned = response_text.strip()
    if not cleaned:
        return None
    if cleaned.upper() == "NO_MATCH":
        return None
    return cleaned


def apply_geography_ai_response(raw_value: str, response_text: str) -> AiFallbackOutcome:
    """Parse geography AI response into normalised value and confidence."""
    parsed = _parse_no_match_response(response_text)
    if parsed is None:
        return AiFallbackOutcome(
            raw_value=raw_value,
            normalised_value=None,
            confidence="none",
            ai_response=response_text,
            note="NO_MATCH",
        )

    whitelist_key = parsed.strip().lower()
    if whitelist_key in GEOGRAPHY_WHITELIST_TO_STANDARD:
        return AiFallbackOutcome(
            raw_value=raw_value,
            normalised_value=GEOGRAPHY_WHITELIST_TO_STANDARD[whitelist_key],
            confidence="Medium",
            ai_response=response_text,
            note="whitelist_corrected",
        )

    return AiFallbackOutcome(
        raw_value=raw_value,
        normalised_value=parsed.strip(),
        confidence="High",
        ai_response=response_text,
        note="real_country_outside_whitelist",
    )


def apply_stage_ai_response(raw_value: str, response_text: str) -> AiFallbackOutcome:
    """Parse stage AI response into normalised value and confidence."""
    parsed = _parse_no_match_response(response_text)
    if parsed is None:
        return AiFallbackOutcome(
            raw_value=raw_value,
            normalised_value=None,
            confidence="none",
            ai_response=response_text,
            note="NO_MATCH",
        )

    canonical = _STAGE_CANONICAL_BY_LOWER.get(parsed.strip().lower())
    if canonical is None:
        return AiFallbackOutcome(
            raw_value=raw_value,
            normalised_value=None,
            confidence="none",
            ai_response=response_text,
            note="NO_MATCH (response not in canonical stage list)",
        )

    return AiFallbackOutcome(
        raw_value=raw_value,
        normalised_value=canonical,
        confidence="Medium",
        ai_response=response_text,
        note="stage_corrected",
    )


def _count_deterministic_none(raw_values: list[Any], normalise_fn: Any) -> int:
    return sum(1 for value in raw_values if normalise_fn(value) is None)


def _count_excluded_blanks(raw_values: list[Any], normalise_fn: Any) -> int:
    return sum(
        1
        for value in raw_values
        if normalise_fn(value) is None and not is_present(value)
    )


def _count_geography_excluded_special(raw_values: list[Any]) -> int:
    return sum(
        1
        for value in raw_values
        if normalise_geography(value) is None
        and is_present(value)
        and _normalise_lookup_key(value) in GEOGRAPHY_AI_EXCLUDED_RAW_VALUES
    )


def run_geography_ai_fallback(
    raw_values: list[Any],
    *,
    client: Any | None = None,
    model: str | None = None,
) -> AiFallbackFieldReport:
    """Run AI fallback for geography values that deterministic normalisation missed."""
    model = model or _get_anthropic_model()
    report = AiFallbackFieldReport(
        field_name="geography",
        deterministic_none_total=_count_deterministic_none(raw_values, normalise_geography),
        excluded_blank=_count_excluded_blanks(raw_values, normalise_geography),
        excluded_special_case=_count_geography_excluded_special(raw_values),
        eligible_for_ai=0,
        unique_values_sent_to_ai=0,
        corrected_medium=0,
        corrected_high=0,
        no_match=0,
        api_errors=0,
    )

    eligible_values = [
        str(value).strip()
        for value in raw_values
        if is_geography_ai_fallback_eligible(value)
    ]
    report.eligible_for_ai = len(eligible_values)

    unique_raw_values = sorted(set(eligible_values), key=str.lower)
    report.unique_values_sent_to_ai = len(unique_raw_values)

    if not unique_raw_values:
        return report

    if client is None:
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is required for AI normalisation "
                f"fallback ({len(unique_raw_values)} unique geography value(s) eligible)."
            )
        client = anthropic.Anthropic(api_key=api_key)

    outcome_by_raw: dict[str, AiFallbackOutcome] = {}
    for raw_value in unique_raw_values:
        try:
            response_text = _call_claude(
                _build_geography_fallback_prompt(raw_value),
                client=client,
                model=model,
            )
            outcome = apply_geography_ai_response(raw_value, response_text)
        except Exception as exc:
            outcome = AiFallbackOutcome(
                raw_value=raw_value,
                normalised_value=None,
                confidence="none",
                ai_response="",
                note=f"API_ERROR: {exc}",
            )
            report.api_errors += 1
        outcome_by_raw[raw_value] = outcome

    for raw_value in eligible_values:
        outcome = outcome_by_raw[raw_value]
        report.outcomes.append(outcome)
        if outcome.note.startswith("API_ERROR"):
            continue
        if outcome.normalised_value is None:
            report.no_match += 1
        elif outcome.confidence == "Medium":
            report.corrected_medium += 1
        elif outcome.confidence == "High":
            report.corrected_high += 1

    return report


def run_stage_ai_fallback(
    raw_values: list[Any],
    *,
    client: Any | None = None,
    model: str | None = None,
) -> AiFallbackFieldReport:
    """Run AI fallback for stage values that deterministic normalisation missed."""
    model = model or _get_anthropic_model()
    report = AiFallbackFieldReport(
        field_name="stage",
        deterministic_none_total=_count_deterministic_none(raw_values, normalise_stage),
        excluded_blank=_count_excluded_blanks(raw_values, normalise_stage),
        excluded_special_case=0,
        eligible_for_ai=0,
        unique_values_sent_to_ai=0,
        corrected_medium=0,
        corrected_high=0,
        no_match=0,
        api_errors=0,
    )

    eligible_values = [
        str(value).strip() for value in raw_values if is_stage_ai_fallback_eligible(value)
    ]
    report.eligible_for_ai = len(eligible_values)

    unique_raw_values = sorted(set(eligible_values), key=str.lower)
    report.unique_values_sent_to_ai = len(unique_raw_values)

    if not unique_raw_values:
        return report

    if client is None:
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable is required for AI normalisation "
                f"fallback ({len(unique_raw_values)} unique stage value(s) eligible)."
            )
        client = anthropic.Anthropic(api_key=api_key)

    outcome_by_raw: dict[str, AiFallbackOutcome] = {}
    for raw_value in unique_raw_values:
        try:
            response_text = _call_claude(
                _build_stage_fallback_prompt(raw_value),
                client=client,
                model=model,
            )
            outcome = apply_stage_ai_response(raw_value, response_text)
        except Exception as exc:
            outcome = AiFallbackOutcome(
                raw_value=raw_value,
                normalised_value=None,
                confidence="none",
                ai_response="",
                note=f"API_ERROR: {exc}",
            )
            report.api_errors += 1
        outcome_by_raw[raw_value] = outcome

    for raw_value in eligible_values:
        outcome = outcome_by_raw[raw_value]
        report.outcomes.append(outcome)
        if outcome.note.startswith("API_ERROR"):
            continue
        if outcome.normalised_value is None:
            report.no_match += 1
        elif outcome.confidence == "Medium":
            report.corrected_medium += 1

    return report
