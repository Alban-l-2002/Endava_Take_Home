"""Stage 2 sector inference via Claude API (terminal output only, no DB writes)."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Literal

from src.classification_rules import SECTOR_INFERENCE_OPTIONS
from src.db_setup import DATABASE_PATH, create_database, get_database_connection
from src.validators import description_has_business_signal, is_present

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DESCRIPTION_TRUNCATE_LENGTH = 60
_RESPONSE_PATTERN = re.compile(
    r"SECTOR:\s*(?P<sector>.+?)\s*\|\s*CONFIDENCE:\s*(?P<confidence>High|Medium|Low)",
    re.IGNORECASE,
)

InferenceConfidence = Literal["High", "Medium", "Low", "none"]

SELECT_MISSING_SECTOR_SQL = """
SELECT
    o.opportunity_id,
    o.company_name,
    o.description,
    r.sector AS sector_raw
FROM vc_opportunities o
JOIN raw_import r ON o.raw_id = r.raw_id
WHERE r.sector IS NULL OR TRIM(r.sector) = ''
ORDER BY o.opportunity_id
"""


@dataclass
class SectorInferenceRecord:
    opportunity_id: int
    company_name: str
    description: str | None
    inferred_sector: str
    confidence: InferenceConfidence
    note: str = ""


@dataclass
class SectorInferenceSummary:
    total_missing_sector: int
    skipped_no_signal: int
    confidence_high: int
    confidence_medium: int
    confidence_low: int
    no_match: int
    api_errors: int = 0


def _get_anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


def _truncate_description(description: Any, max_length: int = DESCRIPTION_TRUNCATE_LENGTH) -> str:
    if not is_present(description):
        return "(blank)"
    text = str(description).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _build_inference_prompt(description: str) -> str:
    sector_list = ", ".join(SECTOR_INFERENCE_OPTIONS)
    return (
        "You are classifying venture capital opportunities by sector.\n\n"
        f"Description: {description}\n\n"
        "Base your decision ONLY on the description above. "
        "Do not use or infer anything from a company name — none is provided.\n\n"
        "Choose the single most likely sector from this exact fixed list only:\n"
        f"{sector_list}\n\n"
        "Also rate your confidence in the choice as High, Medium, or Low.\n"
        "If the description lacks clear sector signal, or none of the listed "
        "sectors are a reasonable fit, return NO_MATCH rather than forcing a choice. "
        "Prefer NO_MATCH or Low confidence when the description is generic or "
        "could apply to many sectors.\n\n"
        "Reply in this exact format only, with no other text:\n"
        "SECTOR: <value> | CONFIDENCE: <value>"
    )


def _parse_inference_response(response_text: str) -> tuple[str, InferenceConfidence | None]:
    match = _RESPONSE_PATTERN.search(response_text.strip())
    if not match:
        return "NO_MATCH", None

    sector = match.group("sector").strip()
    confidence_raw = match.group("confidence").strip().title()
    confidence: InferenceConfidence = confidence_raw  # type: ignore[assignment]

    if sector.upper() == "NO_MATCH":
        return "NO_MATCH", confidence

    if sector not in SECTOR_INFERENCE_OPTIONS:
        return "NO_MATCH", confidence

    return sector, confidence


def _call_claude_for_sector(
    company_name: str,
    description: str,
    *,
    client: Any,
    model: str,
) -> tuple[str, InferenceConfidence]:
    prompt = _build_inference_prompt(description)
    message = client.messages.create(
        model=model,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = ""
    for block in message.content:
        if block.type == "text":
            response_text += block.text

    inferred_sector, confidence = _parse_inference_response(response_text)
    if confidence is None:
        return inferred_sector, "none"
    return inferred_sector, confidence


def _load_missing_sector_records() -> list[dict[str, Any]]:
    create_database()
    with get_database_connection() as conn:
        rows = conn.execute(SELECT_MISSING_SECTOR_SQL).fetchall()
    return [
        {
            "opportunity_id": row[0],
            "company_name": row[1] or "",
            "description": row[2],
            "sector_raw": row[3],
        }
        for row in rows
    ]


def _print_inference_record(result: SectorInferenceRecord) -> None:
    description_display = _truncate_description(result.description)
    note_display = result.note or ""
    print(
        f"{result.company_name} | {description_display} | "
        f"{result.inferred_sector} | {result.confidence} | {note_display}"
    )


def run_sector_inference_dry_run(
    *,
    stream_output: bool = False,
) -> tuple[list[SectorInferenceRecord], SectorInferenceSummary]:
    """
    Infer sectors for missing-sector opportunities; print results to terminal only.

    Read-only database access. No UPDATE or INSERT statements.
    """
    records = _load_missing_sector_records()
    results: list[SectorInferenceRecord] = []
    summary = SectorInferenceSummary(
        total_missing_sector=len(records),
        skipped_no_signal=0,
        confidence_high=0,
        confidence_medium=0,
        confidence_low=0,
        no_match=0,
    )

    if stream_output:
        print_sector_inference_report([], summary, header_only=True)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client: Any | None = None
    model = _get_anthropic_model()
    records_needing_api: list[dict[str, Any]] = []

    for record in records:
        company_name = str(record["company_name"] or "").strip() or "(unknown)"
        description = record["description"]

        if not description_has_business_signal(description):
            result = SectorInferenceRecord(
                opportunity_id=record["opportunity_id"],
                company_name=company_name,
                description=description if is_present(description) else None,
                inferred_sector="SKIPPED_NO_SIGNAL",
                confidence="none",
                note="SKIPPED_NO_SIGNAL",
            )
            results.append(result)
            summary.skipped_no_signal += 1
            if stream_output:
                _print_inference_record(result)
            continue

        records_needing_api.append(record)

    if records_needing_api and not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is required for sector inference "
            f"({len(records_needing_api)} record(s) passed the description signal check)."
        )

    if records_needing_api:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

    for record in records_needing_api:
        company_name = str(record["company_name"] or "").strip() or "(unknown)"
        description = str(record["description"]).strip()

        try:
            inferred_sector, confidence = _call_claude_for_sector(
                company_name,
                description,
                client=client,
                model=model,
            )
            note = ""
        except Exception as exc:
            inferred_sector = "NO_MATCH"
            confidence = "none"
            note = f"API_ERROR: {exc}"
            summary.api_errors += 1

        results.append(
            SectorInferenceRecord(
                opportunity_id=record["opportunity_id"],
                company_name=company_name,
                description=description,
                inferred_sector=inferred_sector,
                confidence=confidence,
                note=note,
            )
        )

        if inferred_sector == "NO_MATCH" and note == "":
            summary.no_match += 1
        elif confidence == "High":
            summary.confidence_high += 1
        elif confidence == "Medium":
            summary.confidence_medium += 1
        elif confidence == "Low":
            summary.confidence_low += 1

        if stream_output:
            _print_inference_record(results[-1])

    return results, summary


def print_sector_inference_report(
    results: list[SectorInferenceRecord],
    summary: SectorInferenceSummary,
    *,
    header_only: bool = False,
) -> None:
    """Print per-record inference lines and summary counts."""
    if header_only:
        print(f"Database path: {DATABASE_PATH}")
        print(f"Anthropic model: {_get_anthropic_model()}")
        print()
        print("company_name | description | inferred_sector | confidence | note")
        print("-" * 120)
        return

    print()
    print("## Sector inference summary")
    print(f"Total missing-sector records:     {summary.total_missing_sector}")
    print(f"Skipped (no description signal):  {summary.skipped_no_signal}")
    print(f"Inferred with High confidence:    {summary.confidence_high}")
    print(f"Inferred with Medium confidence:  {summary.confidence_medium}")
    print(f"Inferred with Low confidence:     {summary.confidence_low}")
    print(f"Returned NO_MATCH:                {summary.no_match}")
    if summary.api_errors:
        print(f"API errors:                       {summary.api_errors}")
    print()


def run_sector_inference() -> tuple[list[SectorInferenceRecord], SectorInferenceSummary]:
    """Run sector inference dry-run and print the report."""
    results, summary = run_sector_inference_dry_run(stream_output=True)
    print_sector_inference_report(results, summary)
    return results, summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 2 sector inference via Claude API (terminal only)."
    )
    return parser.parse_args()


if __name__ == "__main__":
    _parse_args()
    try:
        run_sector_inference()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
