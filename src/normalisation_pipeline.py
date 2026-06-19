"""Stage 2 normalisation pipeline: resolve fields and write audit rows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Literal

from src.ai_normalisation_fallback import (
    AiFallbackOutcome,
    _build_geography_fallback_prompt,
    _build_stage_fallback_prompt,
    _call_claude,
    _get_anthropic_model,
    apply_geography_ai_response,
    apply_stage_ai_response,
    is_geography_ai_fallback_eligible,
    is_stage_ai_fallback_eligible,
)
from src.classification_rules import GEOGRAPHY_AI_EXCLUDED_RAW_VALUES
from src.sector_inference import _build_inference_prompt, _parse_inference_response
from src.validators import (
    description_has_business_signal,
    is_present,
    normalise_geography,
    normalise_stage,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

NormalisationMethod = Literal["deterministic", "ai_fallback", "ai_inferred"]
AuditConfidence = Literal["High", "Medium"]

ProgressCallback = Callable[[str], None]

SELECT_OPPORTUNITIES_FOR_NORMALISATION_SQL = """
SELECT
    o.opportunity_id,
    o.description,
    r.stage,
    r.geography,
    r.sector
FROM vc_opportunities o
JOIN raw_import r ON o.raw_id = r.raw_id
ORDER BY o.opportunity_id
"""

UPDATE_OPPORTUNITY_NORMALISED_SQL = """
UPDATE vc_opportunities
SET
    stage = ?,
    geography = ?,
    sector = ?,
    requires_normalisation_review = ?
WHERE opportunity_id = ?
"""

INSERT_NORMALISATION_AUDIT_SQL = """
INSERT INTO vc_opportunities_normalised (
    opportunity_id,
    field_name,
    original_value,
    method,
    confidence
) VALUES (?, ?, ?, ?, ?)
"""


@dataclass
class NormalisationAudit:
    field_name: Literal["stage", "geography", "sector"]
    original_value: str | None
    method: NormalisationMethod
    confidence: AuditConfidence


@dataclass
class FieldResolution:
    final_value: str | None
    audit: NormalisationAudit | None = None


@dataclass
class Stage2WriteResult:
    opportunities_processed: int
    requires_normalisation_review_count: int
    audit_rows_inserted: int
    audit_by_field_and_method: dict[tuple[str, str], int]
    orphan_audit_rows: int
    stage_deterministic: int = 0
    stage_ai_fallback: int = 0
    stage_unresolved: int = 0
    geography_deterministic: int = 0
    geography_ai_fallback: int = 0
    geography_unresolved: int = 0
    sector_as_is: int = 0
    sector_ai_inferred: int = 0
    sector_unresolved: int = 0
    sector_skipped_no_signal: int = 0
    api_errors: int = 0


def _default_progress(message: str) -> None:
    print(message, flush=True)


def _display_raw(raw_value: Any) -> str:
    return str(raw_value).strip() if is_present(raw_value) else ""


def _values_equivalent(raw_value: Any, normalised_value: str) -> bool:
    return _display_raw(raw_value) == normalised_value.strip()


def _lookup_key(raw_value: Any) -> str:
    return str(raw_value).strip().lower()


def _needs_sector_inference(raw_sector: Any, description: Any) -> bool:
    return not is_present(raw_sector) and description_has_business_signal(description)


def _get_anthropic_client() -> Any:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is required for Stage 2 normalisation "
            "(AI fallback and sector inference)."
        )
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _build_stage_fallback_cache(
    raw_values: list[Any],
    *,
    client: Any,
    model: str,
    on_progress: ProgressCallback | None = None,
) -> dict[str, AiFallbackOutcome]:
    cache: dict[str, AiFallbackOutcome] = {}
    unique_values = sorted(
        {_display_raw(value) for value in raw_values if is_stage_ai_fallback_eligible(value)},
        key=str.lower,
    )
    total = len(unique_values)
    for index, raw_value in enumerate(unique_values, start=1):
        if on_progress:
            on_progress(f"  Stage AI fallback: {index}/{total}")
        try:
            response_text = _call_claude(
                _build_stage_fallback_prompt(raw_value),
                client=client,
                model=model,
            )
            cache[raw_value] = apply_stage_ai_response(raw_value, response_text)
        except Exception as exc:
            cache[raw_value] = AiFallbackOutcome(
                raw_value=raw_value,
                normalised_value=None,
                confidence="none",
                ai_response="",
                note=f"API_ERROR: {exc}",
            )
    return cache


def _build_geography_fallback_cache(
    raw_values: list[Any],
    *,
    client: Any,
    model: str,
    on_progress: ProgressCallback | None = None,
) -> dict[str, AiFallbackOutcome]:
    cache: dict[str, AiFallbackOutcome] = {}
    unique_values = sorted(
        {
            _display_raw(value)
            for value in raw_values
            if is_geography_ai_fallback_eligible(value)
        },
        key=str.lower,
    )
    total = len(unique_values)
    for index, raw_value in enumerate(unique_values, start=1):
        if on_progress:
            on_progress(f"  Geography AI fallback: {index}/{total}")
        try:
            response_text = _call_claude(
                _build_geography_fallback_prompt(raw_value),
                client=client,
                model=model,
            )
            cache[raw_value] = apply_geography_ai_response(raw_value, response_text)
        except Exception as exc:
            cache[raw_value] = AiFallbackOutcome(
                raw_value=raw_value,
                normalised_value=None,
                confidence="none",
                ai_response="",
                note=f"API_ERROR: {exc}",
            )
    return cache


def resolve_stage(
    raw_value: Any,
    *,
    ai_cache: dict[str, AiFallbackOutcome],
) -> FieldResolution:
    if not is_present(raw_value):
        return FieldResolution(None)

    raw_display = _display_raw(raw_value)
    deterministic = normalise_stage(raw_value)
    if deterministic is not None:
        if _values_equivalent(raw_value, deterministic):
            return FieldResolution(deterministic)
        return FieldResolution(
            deterministic,
            NormalisationAudit(
                field_name="stage",
                original_value=raw_display,
                method="deterministic",
                confidence="High",
            ),
        )

    if is_stage_ai_fallback_eligible(raw_value):
        outcome = ai_cache.get(raw_display)
        if outcome and outcome.normalised_value and outcome.confidence == "Medium":
            return FieldResolution(
                outcome.normalised_value,
                NormalisationAudit(
                    field_name="stage",
                    original_value=raw_display,
                    method="ai_fallback",
                    confidence="Medium",
                ),
            )

    return FieldResolution(None)


def resolve_geography(
    raw_value: Any,
    *,
    ai_cache: dict[str, AiFallbackOutcome],
) -> FieldResolution:
    if not is_present(raw_value):
        return FieldResolution(None)

    if _lookup_key(raw_value) in GEOGRAPHY_AI_EXCLUDED_RAW_VALUES:
        return FieldResolution(None)

    raw_display = _display_raw(raw_value)
    deterministic = normalise_geography(raw_value)
    if deterministic is not None:
        if _values_equivalent(raw_value, deterministic):
            return FieldResolution(deterministic)
        return FieldResolution(
            deterministic,
            NormalisationAudit(
                field_name="geography",
                original_value=raw_display,
                method="deterministic",
                confidence="High",
            ),
        )

    if is_geography_ai_fallback_eligible(raw_value):
        outcome = ai_cache.get(raw_display)
        if outcome and outcome.normalised_value and outcome.confidence in {"High", "Medium"}:
            return FieldResolution(
                outcome.normalised_value,
                NormalisationAudit(
                    field_name="geography",
                    original_value=raw_display,
                    method="ai_fallback",
                    confidence=outcome.confidence,  # type: ignore[arg-type]
                ),
            )

    return FieldResolution(None)


def infer_sector_from_description(
    description: Any,
    *,
    client: Any,
    model: str,
) -> FieldResolution:
    if not description_has_business_signal(description):
        return FieldResolution(None)

    prompt = _build_inference_prompt(str(description).strip())
    try:
        response_text = _call_claude(prompt, client=client, model=model)
        inferred_sector, confidence = _parse_inference_response(response_text)
    except Exception:
        return FieldResolution(None)

    if inferred_sector == "NO_MATCH" or confidence not in {"High", "Medium"}:
        return FieldResolution(None)

    return FieldResolution(
        inferred_sector,
        NormalisationAudit(
            field_name="sector",
            original_value=None,
            method="ai_inferred",
            confidence=confidence,  # type: ignore[arg-type]
        ),
    )


def resolve_sector(
    raw_value: Any,
    description: Any,
    *,
    sector_cache: dict[int, FieldResolution],
    opportunity_id: int,
) -> FieldResolution:
    if is_present(raw_value):
        return FieldResolution(_display_raw(raw_value))
    return sector_cache.get(opportunity_id, FieldResolution(None))


def run_stage2_normalisation_write(
    conn: Any,
    *,
    show_progress: bool = True,
) -> Stage2WriteResult:
    """Apply normalisation and persist final values plus audit rows."""
    log: ProgressCallback = _default_progress if show_progress else lambda _msg: None

    rows = conn.execute(SELECT_OPPORTUNITIES_FOR_NORMALISATION_SQL).fetchall()
    total_records = len(rows)
    model = _get_anthropic_model()
    client = _get_anthropic_client()

    stage_raw_values = [row[2] for row in rows]
    geography_raw_values = [row[3] for row in rows]

    log(f"Stage 2: processing {total_records} opportunities")
    log(f"Step 1/4: Deterministic normalisation (stage & geography)...")

    stage_deterministic_count = 0
    geography_deterministic_count = 0
    sector_as_is_count = 0
    for row in rows:
        raw_stage = row[2]
        raw_geography = row[3]
        raw_sector = row[4]

        stage_det = normalise_stage(raw_stage)
        if stage_det is not None and is_present(raw_stage) and not _values_equivalent(
            raw_stage, stage_det
        ):
            stage_deterministic_count += 1

        if _lookup_key(raw_geography) not in GEOGRAPHY_AI_EXCLUDED_RAW_VALUES:
            geo_det = normalise_geography(raw_geography)
            if geo_det is not None and is_present(raw_geography) and not _values_equivalent(
                raw_geography, geo_det
            ):
                geography_deterministic_count += 1

        if is_present(raw_sector):
            sector_as_is_count += 1

    log(
        f"  Done. {stage_deterministic_count} stage and "
        f"{geography_deterministic_count} geography values will be standardised; "
        f"{sector_as_is_count} sectors already present in raw data."
    )

    stage_fallback_total = len(
        {_display_raw(v) for v in stage_raw_values if is_stage_ai_fallback_eligible(v)}
    )
    geography_fallback_total = len(
        {_display_raw(v) for v in geography_raw_values if is_geography_ai_fallback_eligible(v)}
    )

    log(
        f"Step 2/4: AI fallback for spelling/formatting "
        f"({stage_fallback_total} stage values, {geography_fallback_total} geography values)..."
    )
    if stage_fallback_total == 0 and geography_fallback_total == 0:
        log("  Skipped — no values eligible for AI fallback.")
        stage_ai_cache: dict[str, AiFallbackOutcome] = {}
        geography_ai_cache: dict[str, AiFallbackOutcome] = {}
    else:
        stage_ai_cache = _build_stage_fallback_cache(
            stage_raw_values,
            client=client,
            model=model,
            on_progress=log if stage_fallback_total else None,
        )
        geography_ai_cache = _build_geography_fallback_cache(
            geography_raw_values,
            client=client,
            model=model,
            on_progress=log if geography_fallback_total else None,
        )
        log("  AI fallback complete.")

    inference_rows = [
        row for row in rows if _needs_sector_inference(row[4], row[1])
    ]
    inference_total = len(inference_rows)
    log(f"Step 3/4: Sector inference ({inference_total} records)...")

    sector_cache: dict[int, FieldResolution] = {}
    if inference_total == 0:
        log("  Skipped — no records need sector inference.")
    else:
        for index, row in enumerate(inference_rows, start=1):
            opportunity_id = row[0]
            description = row[1]
            sector_cache[opportunity_id] = infer_sector_from_description(
                description,
                client=client,
                model=model,
            )
            log(f"  Sector inference: {index}/{inference_total}")
        log("  Sector inference complete.")

    log(f"Step 4/4: Writing results to database ({total_records} records)...")
    conn.execute("DELETE FROM vc_opportunities_normalised")

    result = Stage2WriteResult(
        opportunities_processed=total_records,
        requires_normalisation_review_count=0,
        audit_rows_inserted=0,
        audit_by_field_and_method={},
        orphan_audit_rows=0,
    )

    for row in rows:
        opportunity_id = row[0]
        description = row[1]
        raw_stage = row[2]
        raw_geography = row[3]
        raw_sector = row[4]

        stage_resolution = resolve_stage(raw_stage, ai_cache=stage_ai_cache)
        geography_resolution = resolve_geography(raw_geography, ai_cache=geography_ai_cache)
        sector_resolution = resolve_sector(
            raw_sector,
            description,
            sector_cache=sector_cache,
            opportunity_id=opportunity_id,
        )

        if is_present(raw_sector):
            result.sector_as_is += 1
        elif not description_has_business_signal(description):
            result.sector_skipped_no_signal += 1
            result.sector_unresolved += 1
        elif sector_resolution.final_value is None:
            result.sector_unresolved += 1
        else:
            result.sector_ai_inferred += 1

        if stage_resolution.final_value is None:
            result.stage_unresolved += 1
        elif stage_resolution.audit and stage_resolution.audit.method == "deterministic":
            result.stage_deterministic += 1
        elif stage_resolution.audit and stage_resolution.audit.method == "ai_fallback":
            result.stage_ai_fallback += 1

        if geography_resolution.final_value is None:
            result.geography_unresolved += 1
        elif geography_resolution.audit and geography_resolution.audit.method == "deterministic":
            result.geography_deterministic += 1
        elif geography_resolution.audit and geography_resolution.audit.method == "ai_fallback":
            result.geography_ai_fallback += 1

        audits = [
            stage_resolution.audit,
            geography_resolution.audit,
            sector_resolution.audit,
        ]
        active_audits = [audit for audit in audits if audit is not None]
        requires_normalisation_review = 1 if active_audits else 0
        if requires_normalisation_review:
            result.requires_normalisation_review_count += 1

        conn.execute(
            UPDATE_OPPORTUNITY_NORMALISED_SQL,
            (
                stage_resolution.final_value,
                geography_resolution.final_value,
                sector_resolution.final_value,
                requires_normalisation_review,
                opportunity_id,
            ),
        )

        for audit in active_audits:
            conn.execute(
                INSERT_NORMALISATION_AUDIT_SQL,
                (
                    opportunity_id,
                    audit.field_name,
                    audit.original_value,
                    audit.method,
                    audit.confidence,
                ),
            )
            result.audit_rows_inserted += 1
            key = (audit.field_name, audit.method)
            result.audit_by_field_and_method[key] = (
                result.audit_by_field_and_method.get(key, 0) + 1
            )

    log("  Database write complete.")

    result.api_errors = sum(
        1
        for outcome in {**stage_ai_cache, **geography_ai_cache}.values()
        if outcome.note.startswith("API_ERROR")
    )

    opportunity_ids = {
        row[0] for row in conn.execute("SELECT opportunity_id FROM vc_opportunities").fetchall()
    }
    audit_opportunity_ids = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT opportunity_id FROM vc_opportunities_normalised"
        ).fetchall()
    }
    result.orphan_audit_rows = len(audit_opportunity_ids - opportunity_ids)

    return result
