"""Stage 2: normalisation, confidence ratings, and controlled AI inference."""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from src.ai_normalisation_fallback import (
    AiFallbackFieldReport,
    _get_anthropic_model,
    run_geography_ai_fallback,
    run_stage_ai_fallback,
)
from src.classification_rules import (
    GEOGRAPHY_NORMALISATION_MAP,
    STAGE_NORMALISATION_MAP,
)
from src.db_setup import DATABASE_PATH, create_database, get_database_connection
from src.normalisation_pipeline import Stage2WriteResult, run_stage2_normalisation_write
from src.validators import is_present, normalise_geography, normalise_stage

_LABEL_WIDTH = 86
_BLANK_DISPLAY = "(blank)"

SELECT_OPPORTUNITIES_NORMALISATION_SQL = """
SELECT
    o.opportunity_id,
    r.stage,
    r.geography
FROM vc_opportunities o
JOIN raw_import r ON o.raw_id = r.raw_id
ORDER BY o.opportunity_id
"""


@dataclass
class FieldNormalisationReport:
    field_name: str
    total_records: int
    normalised_count: int
    none_count: int
    raw_to_normalised_counts: Counter[tuple[str, str]]
    unmapped_raw_counts: Counter[str]

    @property
    def reconciliation_passed(self) -> bool:
        return self.normalised_count + self.none_count == self.total_records


@dataclass
class Stage2DryRunResult:
    total_records_processed: int
    stage_report: FieldNormalisationReport
    geography_report: FieldNormalisationReport
    stage_ai_report: AiFallbackFieldReport
    geography_ai_report: AiFallbackFieldReport


def _display_raw_value(raw_value: Any) -> str:
    if not is_present(raw_value):
        return _BLANK_DISPLAY
    return str(raw_value)


def _display_normalised_value(raw_value: Any, normalised_value: str | None) -> str:
    if normalised_value is not None:
        return normalised_value
    if not is_present(raw_value):
        return _BLANK_DISPLAY
    return "None"


def _build_field_report(
    field_name: str,
    raw_values: list[Any],
    normalise_fn: Any,
    normalisation_map: dict[str, str | None],
) -> FieldNormalisationReport:
    raw_to_normalised_counts: Counter[tuple[str, str]] = Counter()
    unmapped_raw_counts: Counter[str] = Counter()
    normalised_count = 0
    none_count = 0

    for raw_value in raw_values:
        normalised_value = normalise_fn(raw_value)
        display_raw = _display_raw_value(raw_value)
        display_normalised = _display_normalised_value(raw_value, normalised_value)
        raw_to_normalised_counts[(display_raw, display_normalised)] += 1

        if normalised_value is None:
            none_count += 1
            if is_present(raw_value):
                lookup_key = str(raw_value).strip().lower()
                if lookup_key not in normalisation_map:
                    unmapped_raw_counts[display_raw] += 1
        else:
            normalised_count += 1

    return FieldNormalisationReport(
        field_name=field_name,
        total_records=len(raw_values),
        normalised_count=normalised_count,
        none_count=none_count,
        raw_to_normalised_counts=raw_to_normalised_counts,
        unmapped_raw_counts=unmapped_raw_counts,
    )


def run_stage2_dry_run() -> Stage2DryRunResult:
    """
    Preview deterministic and AI fallback normalisation in memory (read-only).

    Reads raw stage/geography values from raw_import via join; no writes.
    """
    create_database()

    with get_database_connection() as conn:
        rows = conn.execute(SELECT_OPPORTUNITIES_NORMALISATION_SQL).fetchall()

    stage_values = [row[1] for row in rows]
    geography_values = [row[2] for row in rows]

    stage_report = _build_field_report(
        field_name="stage",
        raw_values=stage_values,
        normalise_fn=normalise_stage,
        normalisation_map=STAGE_NORMALISATION_MAP,
    )
    geography_report = _build_field_report(
        field_name="geography",
        raw_values=geography_values,
        normalise_fn=normalise_geography,
        normalisation_map=GEOGRAPHY_NORMALISATION_MAP,
    )

    stage_ai_report = run_stage_ai_fallback(stage_values)
    geography_ai_report = run_geography_ai_fallback(geography_values)

    return Stage2DryRunResult(
        total_records_processed=len(rows),
        stage_report=stage_report,
        geography_report=geography_report,
        stage_ai_report=stage_ai_report,
        geography_ai_report=geography_ai_report,
    )


def run_stage2_write(*, show_progress: bool = True) -> Stage2WriteResult:
    """Apply normalisation and persist final field values plus audit rows."""
    create_database()
    with get_database_connection() as conn:
        try:
            result = run_stage2_normalisation_write(conn, show_progress=show_progress)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


def _print_metric(label: str, value: int | str) -> None:
    print(f"{label:<{_LABEL_WIDTH}} {value}")


def _print_section(title: str) -> None:
    print(f"\n## {title}")


def _print_field_report(report: FieldNormalisationReport) -> None:
    _print_section(f"{report.field_name.title()} normalisation (deterministic preview)")
    _print_metric("Successfully normalised", report.normalised_count)
    _print_metric("Returned None", report.none_count)
    _print_metric("Reconciliation", "PASS" if report.reconciliation_passed else "FAIL")


def _print_ai_fallback_report(report: AiFallbackFieldReport) -> None:
    _print_section(f"{report.field_name.title()} AI fallback preview")
    _print_metric("Records eligible for AI fallback", report.eligible_for_ai)
    _print_metric("Unique raw values sent to AI", report.unique_values_sent_to_ai)
    _print_metric("Corrected (Medium confidence)", report.corrected_medium)
    if report.field_name == "geography":
        _print_metric("Accepted real country outside whitelist (High)", report.corrected_high)
    _print_metric("NO_MATCH (left as None)", report.no_match)


def print_stage2_dry_run_report(result: Stage2DryRunResult) -> None:
    """Print the Stage 2 dry-run normalisation report."""
    print(f"Database path: {DATABASE_PATH}")
    print(f"Anthropic model (AI fallback): {_get_anthropic_model()}")
    _print_section("Stage 2 dry-run summary")
    _print_metric("Total records processed", result.total_records_processed)
    _print_field_report(result.stage_report)
    _print_field_report(result.geography_report)
    _print_ai_fallback_report(result.stage_ai_report)
    _print_ai_fallback_report(result.geography_ai_report)
    print()


def print_stage2_write_report(result: Stage2WriteResult) -> None:
    """Print reconciliation report after Stage 2 writes."""
    print(f"Database path: {DATABASE_PATH}")
    print(f"Anthropic model: {_get_anthropic_model()}")
    _print_section("Stage 2 write reconciliation")
    _print_metric("Total opportunities processed", result.opportunities_processed)
    _print_metric(
        "Opportunities with requires_normalisation_review=1",
        result.requires_normalisation_review_count,
    )
    _print_metric("Total audit rows in vc_opportunities_normalised", result.audit_rows_inserted)
    print()
    print("Audit rows by field_name and method")
    if not result.audit_by_field_and_method:
        _print_metric("  (none)", 0)
    else:
        for (field_name, method), count in sorted(result.audit_by_field_and_method.items()):
            _print_metric(f"  {field_name} / {method}", count)

    print()
    _print_section("Field resolution summary")
    _print_metric("Stage corrected (deterministic)", result.stage_deterministic)
    _print_metric("Stage corrected (AI fallback)", result.stage_ai_fallback)
    _print_metric("Stage unresolved (NULL final)", result.stage_unresolved)
    _print_metric("Geography corrected (deterministic)", result.geography_deterministic)
    _print_metric("Geography corrected (AI fallback)", result.geography_ai_fallback)
    _print_metric("Geography unresolved (NULL final)", result.geography_unresolved)
    _print_metric("Sector kept as raw (no transform)", result.sector_as_is)
    _print_metric("Sector inferred (AI)", result.sector_ai_inferred)
    _print_metric("Sector skipped (no description signal)", result.sector_skipped_no_signal)
    _print_metric("Sector unresolved (NULL final)", result.sector_unresolved)
    if result.api_errors:
        _print_metric("AI API errors (fallback cache)", result.api_errors)

    orphan_status = "PASS" if result.orphan_audit_rows == 0 else "FAIL"
    print()
    _print_metric("Orphan audit rows (invalid opportunity_id)", result.orphan_audit_rows)
    _print_metric("Audit FK reconciliation", orphan_status)
    print()


def run_stage2(*, dry_run: bool = False, show_progress: bool = True) -> Stage2DryRunResult | Stage2WriteResult:
    """Run Stage 2 normalisation. Writes to the database unless dry_run=True."""
    if dry_run:
        result = run_stage2_dry_run()
        print_stage2_dry_run_report(result)
        return result

    result = run_stage2_write(show_progress=show_progress)
    print_stage2_write_report(result)
    return result


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    quiet = "--quiet" in sys.argv
    try:
        run_stage2(dry_run=dry_run, show_progress=not quiet)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
