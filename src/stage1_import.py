"""Stage 1: CSV import and record classification."""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.db_setup import DATABASE_PATH, create_database, get_database_connection
from src.validators import (
    DuplicateDetectionResult,
    RecordClassificationStatus,
    SuspectedDuplicatePair,
    build_review_assignment,
    classify_record,
    detect_duplicate_suspects,
    get_duplicate_confidence_tier,
    get_invalid_field_details,
    classify_founder_background,
    classify_geography,
    classify_referral_source,
    classify_sector,
    classify_stage,
    classify_traction,
    is_all_fields_valid,
    is_fully_empty_row,
    is_present,
    is_geography_ambiguous,
    is_geography_invalid,
    is_referral_invalid,
    is_sector_ambiguous,
    is_sector_invalid,
    is_sector_invalid_from_stage_contamination,
    is_stage_invalid_from_sector_contamination,
    is_stage_incomplete_or_unknown,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_PATH = PROJECT_ROOT / "data" / "vc_opportunities_dataset.csv"

_LABEL_WIDTH = 86

EXPECTED_COLUMNS = [
    "company_name",
    "description",
    "sector",
    "stage",
    "geography",
    "traction",
    "founder_background",
    "referral_source",
]

INSERT_SQL = """
INSERT INTO raw_import (
    source_row_number,
    company_name,
    description,
    sector,
    stage,
    geography,
    traction,
    founder_background,
    referral_source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SELECT_CLASSIFICATION_FIELDS_SQL = """
SELECT
    raw_id,
    company_name,
    description,
    sector,
    stage,
    geography,
    traction,
    founder_background,
    referral_source
FROM raw_import
"""

CLASSIFICATION_COLUMNS = [
    "raw_id",
    *EXPECTED_COLUMNS,
]

INSERT_OPPORTUNITY_SQL = """
INSERT INTO vc_opportunities (
    raw_id,
    company_name,
    description,
    traction,
    founder_background,
    referral_source,
    validation_status,
    validation_reason,
    requires_review,
    review_reason,
    review_priority,
    duplicate_suspected
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_EXCEPTION_SQL = """
INSERT INTO vc_opportunity_exceptions (
    raw_id,
    exception_type,
    exception_reason,
    affected_field,
    affected_value
) VALUES (?, ?, ?, ?, ?)
"""

INSERT_SUSPECTED_DUPLICATE_SQL = """
INSERT INTO suspected_duplicates (
    opportunity_id_a,
    opportunity_id_b,
    company_name,
    confidence_tier,
    matching_fields,
    matching_field_count
) VALUES (?, ?, ?, ?, ?, ?)
"""

EMPTY_ROW_EXCEPTION_REASON = "Completely empty row, no data present"


@dataclass
class SuspectedDuplicatesResult:
    high_pairs_inserted: int
    medium_pairs_inserted: int
    high_pairs_skipped: int
    medium_pairs_skipped: int
    expected_high_pairs: int
    expected_medium_pairs: int

    @property
    def total_pairs_inserted(self) -> int:
        return self.high_pairs_inserted + self.medium_pairs_inserted

    @property
    def pairs_skipped_missing_opportunity(self) -> int:
        return self.high_pairs_skipped + self.medium_pairs_skipped

    @property
    def reconciliation_passed(self) -> bool:
        return (
            self.high_pairs_inserted + self.high_pairs_skipped
            == self.expected_high_pairs
            and self.medium_pairs_inserted + self.medium_pairs_skipped
            == self.expected_medium_pairs
        )


@dataclass
class RoutingResult:
    opportunities_inserted: int
    exceptions_inserted: int
    total_rows_routed: int

    @property
    def reconciliation_passed(self) -> bool:
        return self.opportunities_inserted + self.exceptions_inserted == self.total_rows_routed


@dataclass
class Stage1Result:
    total_rows_checked: int
    fully_empty_rows: int
    company_records_classified: int
    valid: int
    incomplete: int
    ambiguous: int
    invalid: int
    all_fields_valid: int
    missing_company_names: int
    missing_descriptions: int
    missing_or_unknown_stages: int
    missing_sectors: int
    invalid_sectors: int
    ambiguous_sectors: int
    invalid_stages_from_sector_contamination: int
    invalid_sectors_from_stage_contamination: int
    missing_founder_backgrounds: int
    missing_traction_values: int
    missing_geographies: int
    ambiguous_geographies: int
    invalid_geographies: int
    missing_referral_sources: int
    invalid_referral_sources: int
    records_with_incomplete_mandatory_field: int
    records_with_ambiguous_field: int
    records_with_invalid_field: int
    repeated_company_name_groups: int
    high_confidence_pairs: int
    medium_confidence_pairs: int
    low_confidence_pairs: int
    unique_records_high_confidence: int
    unique_records_medium_confidence: int
    unique_records_low_confidence: int
    unique_records_duplicate_suspected: int
    unique_records_possible_name_collision: int
    duplicate_suspected_valid: int
    duplicate_suspected_incomplete: int
    duplicate_suspected_ambiguous: int
    duplicate_suspected_invalid: int
    possible_name_collision_valid: int
    possible_name_collision_incomplete: int
    possible_name_collision_ambiguous: int
    possible_name_collision_invalid: int
    duplicate_suspected_queue_count: int
    opportunities_inserted: int = 0
    exceptions_inserted: int = 0
    routing_reconciliation_passed: bool = False
    suspected_duplicates_high_inserted: int = 0
    suspected_duplicates_medium_inserted: int = 0
    suspected_duplicates_high_skipped: int = 0
    suspected_duplicates_medium_skipped: int = 0
    suspected_duplicates_reconciliation_passed: bool = False

    @property
    def duplicate_suspected_count(self) -> int:
        return (
            self.duplicate_suspected_valid
            + self.duplicate_suspected_incomplete
            + self.duplicate_suspected_ambiguous
            + self.duplicate_suspected_invalid
        )

    @property
    def possible_name_collision_count(self) -> int:
        return (
            self.possible_name_collision_valid
            + self.possible_name_collision_incomplete
            + self.possible_name_collision_ambiguous
            + self.possible_name_collision_invalid
        )

    @property
    def rows_retained_in_staging(self) -> int:
        return self.total_rows_checked

    @property
    def reconciliation_passed(self) -> bool:
        classified_total = (
            self.fully_empty_rows
            + self.valid
            + self.incomplete
            + self.ambiguous
            + self.invalid
        )
        company_total = (
            self.valid + self.incomplete + self.ambiguous + self.invalid
        )
        return (
            classified_total == self.total_rows_checked
            and company_total == self.company_records_classified
        )


def _to_sqlite_value(value: Any) -> Any:
    """Convert pandas NaN values to None for SQLite insertion."""
    if pd.isna(value):
        return None
    return value


def get_staging_row_count() -> int:
    """Return the number of records currently in raw_import."""
    with get_database_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM raw_import").fetchone()[0]


def import_csv_to_staging(csv_path: str | Path) -> int:
    """
    Import CSV rows into raw_import without cleaning or transforming values.

    Returns the number of rows inserted.
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    df = pd.read_csv(csv_path)

    missing_columns = [column for column in EXPECTED_COLUMNS if column not in df.columns]
    if missing_columns:
        raise ValueError(
            "CSV is missing required columns: " + ", ".join(missing_columns)
        )

    with get_database_connection() as conn:
        existing_count = conn.execute("SELECT COUNT(*) FROM raw_import").fetchone()[0]
        if existing_count > 0:
            raise RuntimeError(
                "The raw_import staging table is not empty "
                f"({existing_count} record(s) present). "
                "Clear the table or remove the database before re-importing. "
                "Multi-batch import support will be introduced in a later version."
            )

        rows_to_insert = [
            (
                int(index) + 2,
                _to_sqlite_value(row["company_name"]),
                _to_sqlite_value(row["description"]),
                _to_sqlite_value(row["sector"]),
                _to_sqlite_value(row["stage"]),
                _to_sqlite_value(row["geography"]),
                _to_sqlite_value(row["traction"]),
                _to_sqlite_value(row["founder_background"]),
                _to_sqlite_value(row["referral_source"]),
            )
            for index, row in df.iterrows()
        ]

        try:
            conn.executemany(INSERT_SQL, rows_to_insert)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return len(rows_to_insert)


def import_csv_force(csv_path: str | Path = DEFAULT_CSV_PATH) -> int:
    """Clear staging and import the CSV from scratch."""
    with get_database_connection() as conn:
        conn.execute("DELETE FROM raw_import")
        conn.commit()
    return import_csv_to_staging(csv_path)


def import_csv_if_empty(csv_path: str | Path = DEFAULT_CSV_PATH) -> int | None:
    """
    Import the CSV only when raw_import is empty.

    Returns the number of rows inserted, or None if import was skipped.
    """
    if get_staging_row_count() > 0:
        return None
    return import_csv_to_staging(csv_path)


def _row_to_record(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(CLASSIFICATION_COLUMNS, row, strict=True))


def _print_metric(label: str, value: int | str) -> None:
    print(f"{label:<{_LABEL_WIDTH}} {value}")


def _print_section(title: str) -> None:
    print(f"\n## {title}")


def _has_incomplete_mandatory_field(record: dict[str, Any]) -> bool:
    """Return True when any mandatory field is classified as Incomplete."""
    if not is_present(record.get("company_name")):
        return True
    mandatory_classifiers = (
        classify_sector(record.get("sector")),
        classify_stage(record.get("stage")),
        classify_geography(record.get("geography")),
        classify_founder_background(record.get("founder_background")),
        classify_traction(record.get("traction")),
        classify_referral_source(record.get("referral_source")),
    )
    return any(status == "Incomplete" for status in mandatory_classifiers)


def _has_ambiguous_field(record: dict[str, Any]) -> bool:
    """Return True when any validated field is classified as Ambiguous."""
    ambiguous_classifiers = (
        classify_sector(record.get("sector")),
        classify_stage(record.get("stage")),
        classify_geography(record.get("geography")),
    )
    return any(status == "Ambiguous" for status in ambiguous_classifiers)


def _has_invalid_field(record: dict[str, Any]) -> bool:
    """Return True when any validated field is classified as Invalid."""
    invalid_classifiers = (
        classify_sector(record.get("sector")),
        classify_stage(record.get("stage")),
        classify_geography(record.get("geography")),
        classify_referral_source(record.get("referral_source")),
    )
    return any(status == "Invalid" for status in invalid_classifiers)


def classify_stage1() -> Stage1Result:
    """
    Read all staged records and classify empty rows and company records.

    Checks fully empty rows, company name, description (field quality only),
    sector, stage, geography, founder background, traction, and referral source.
    Other columns are not validated yet.
    """
    with get_database_connection() as conn:
        rows = conn.execute(SELECT_CLASSIFICATION_FIELDS_SQL).fetchall()

    staged_records = [_row_to_record(row) for row in rows]
    return _build_stage1_result(staged_records)


def _build_stage1_result(staged_records: list[dict[str, Any]]) -> Stage1Result:
    record_counts: Counter[RecordClassificationStatus] = Counter(
        Valid=0,
        Incomplete=0,
        Ambiguous=0,
        Invalid=0,
    )
    fully_empty_rows = 0
    missing_company_names = 0
    missing_descriptions = 0
    missing_or_unknown_stages = 0
    missing_sectors = 0
    invalid_sectors = 0
    ambiguous_sectors = 0
    invalid_stages_from_sector_contamination = 0
    invalid_sectors_from_stage_contamination = 0
    missing_founder_backgrounds = 0
    missing_traction_values = 0
    missing_geographies = 0
    ambiguous_geographies = 0
    invalid_geographies = 0
    missing_referral_sources = 0
    invalid_referral_sources = 0
    all_fields_valid = 0
    records_with_incomplete_mandatory_field = 0
    records_with_ambiguous_field = 0
    records_with_invalid_field = 0

    for record in staged_records:
        if is_fully_empty_row(record):
            fully_empty_rows += 1
            continue

        if not is_present(record.get("company_name")):
            missing_company_names += 1
        if not is_present(record.get("description")):
            missing_descriptions += 1
        if is_stage_incomplete_or_unknown(record.get("stage")):
            missing_or_unknown_stages += 1
        if not is_present(record.get("geography")):
            missing_geographies += 1
        if is_geography_ambiguous(record.get("geography")):
            ambiguous_geographies += 1
        if is_geography_invalid(record.get("geography")):
            invalid_geographies += 1
        if not is_present(record.get("sector")):
            missing_sectors += 1
        if is_sector_invalid(record.get("sector")):
            invalid_sectors += 1
        if is_sector_ambiguous(record.get("sector")):
            ambiguous_sectors += 1
        if is_stage_invalid_from_sector_contamination(record.get("stage")):
            invalid_stages_from_sector_contamination += 1
        if is_sector_invalid_from_stage_contamination(record.get("sector")):
            invalid_sectors_from_stage_contamination += 1
        if not is_present(record.get("founder_background")):
            missing_founder_backgrounds += 1
        if not is_present(record.get("traction")):
            missing_traction_values += 1
        if not is_present(record.get("referral_source")):
            missing_referral_sources += 1
        if is_referral_invalid(record.get("referral_source")):
            invalid_referral_sources += 1

        record_counts[classify_record(record)] += 1
        if is_all_fields_valid(record):
            all_fields_valid += 1
        if _has_incomplete_mandatory_field(record):
            records_with_incomplete_mandatory_field += 1
        if _has_ambiguous_field(record):
            records_with_ambiguous_field += 1
        if _has_invalid_field(record):
            records_with_invalid_field += 1

    company_records_classified = sum(record_counts.values())
    duplicate_result = detect_duplicate_suspects(staged_records)

    return Stage1Result(
        total_rows_checked=len(staged_records),
        fully_empty_rows=fully_empty_rows,
        company_records_classified=company_records_classified,
        valid=record_counts["Valid"],
        incomplete=record_counts["Incomplete"],
        ambiguous=record_counts["Ambiguous"],
        invalid=record_counts["Invalid"],
        all_fields_valid=all_fields_valid,
        missing_company_names=missing_company_names,
        missing_descriptions=missing_descriptions,
        missing_or_unknown_stages=missing_or_unknown_stages,
        missing_sectors=missing_sectors,
        invalid_sectors=invalid_sectors,
        ambiguous_sectors=ambiguous_sectors,
        invalid_stages_from_sector_contamination=invalid_stages_from_sector_contamination,
        invalid_sectors_from_stage_contamination=invalid_sectors_from_stage_contamination,
        missing_founder_backgrounds=missing_founder_backgrounds,
        missing_traction_values=missing_traction_values,
        missing_geographies=missing_geographies,
        ambiguous_geographies=ambiguous_geographies,
        invalid_geographies=invalid_geographies,
        missing_referral_sources=missing_referral_sources,
        invalid_referral_sources=invalid_referral_sources,
        records_with_incomplete_mandatory_field=records_with_incomplete_mandatory_field,
        records_with_ambiguous_field=records_with_ambiguous_field,
        records_with_invalid_field=records_with_invalid_field,
        repeated_company_name_groups=duplicate_result.repeated_company_name_groups,
        high_confidence_pairs=duplicate_result.high_confidence_pairs,
        medium_confidence_pairs=duplicate_result.medium_confidence_pairs,
        low_confidence_pairs=duplicate_result.low_confidence_pairs,
        unique_records_high_confidence=duplicate_result.unique_records_high_confidence,
        unique_records_medium_confidence=duplicate_result.unique_records_medium_confidence,
        unique_records_low_confidence=duplicate_result.unique_records_low_confidence,
        unique_records_duplicate_suspected=duplicate_result.unique_records_duplicate_suspected,
        unique_records_possible_name_collision=duplicate_result.unique_records_possible_name_collision,
        duplicate_suspected_valid=duplicate_result.duplicate_suspected_valid,
        duplicate_suspected_incomplete=duplicate_result.duplicate_suspected_incomplete,
        duplicate_suspected_ambiguous=duplicate_result.duplicate_suspected_ambiguous,
        duplicate_suspected_invalid=duplicate_result.duplicate_suspected_invalid,
        possible_name_collision_valid=duplicate_result.possible_name_collision_valid,
        possible_name_collision_incomplete=duplicate_result.possible_name_collision_incomplete,
        possible_name_collision_ambiguous=duplicate_result.possible_name_collision_ambiguous,
        possible_name_collision_invalid=duplicate_result.possible_name_collision_invalid,
        duplicate_suspected_queue_count=duplicate_result.duplicate_suspected_queue_count,
    )


def route_stage1_records(
    staged_records: list[dict[str, Any]],
    duplicate_result: DuplicateDetectionResult,
) -> RoutingResult:
    """Route classified staging records into vc_opportunities or vc_opportunity_exceptions."""
    opportunities_inserted = 0
    exceptions_inserted = 0

    with get_database_connection() as conn:
        conn.execute("DELETE FROM vc_opportunities_normalised")
        conn.execute("DELETE FROM suspected_duplicates")
        conn.execute("DELETE FROM vc_opportunity_exceptions")
        conn.execute("DELETE FROM vc_opportunities")

        try:
            for record in staged_records:
                raw_id = record["raw_id"]

                if is_fully_empty_row(record):
                    conn.execute(
                        INSERT_EXCEPTION_SQL,
                        (
                            raw_id,
                            "Invalid",
                            EMPTY_ROW_EXCEPTION_REASON,
                            None,
                            None,
                        ),
                    )
                    exceptions_inserted += 1
                    continue

                validation_status = classify_record(record)

                if validation_status == "Invalid":
                    exception_reason, affected_field, affected_value = (
                        get_invalid_field_details(record)
                    )
                    conn.execute(
                        INSERT_EXCEPTION_SQL,
                        (
                            raw_id,
                            "Invalid",
                            exception_reason,
                            affected_field,
                            affected_value,
                        ),
                    )
                    exceptions_inserted += 1
                    continue

                duplicate_tier = get_duplicate_confidence_tier(raw_id, duplicate_result)
                duplicate_suspected = 1 if duplicate_tier is not None else 0
                requires_review, review_reason, review_priority = build_review_assignment(
                    record,
                    duplicate_tier,
                )

                conn.execute(
                    INSERT_OPPORTUNITY_SQL,
                    (
                        raw_id,
                        record.get("company_name"),
                        record.get("description"),
                        record.get("traction"),
                        record.get("founder_background"),
                        record.get("referral_source"),
                        validation_status,
                        None,
                        requires_review,
                        review_reason,
                        review_priority,
                        duplicate_suspected,
                    ),
                )
                opportunities_inserted += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return RoutingResult(
        opportunities_inserted=opportunities_inserted,
        exceptions_inserted=exceptions_inserted,
        total_rows_routed=len(staged_records),
    )


def persist_suspected_duplicates(
    suspected_pairs: tuple[SuspectedDuplicatePair, ...],
    duplicate_result: DuplicateDetectionResult,
) -> SuspectedDuplicatesResult:
    """Persist High and Medium duplicate pairs after vc_opportunities routing."""
    high_pairs_inserted = 0
    medium_pairs_inserted = 0
    high_pairs_skipped = 0
    medium_pairs_skipped = 0

    with get_database_connection() as conn:
        raw_id_to_opportunity_id = dict(
            conn.execute(
                "SELECT raw_id, opportunity_id FROM vc_opportunities"
            ).fetchall()
        )

        try:
            for pair in suspected_pairs:
                opportunity_id_a = raw_id_to_opportunity_id.get(pair.left_raw_id)
                opportunity_id_b = raw_id_to_opportunity_id.get(pair.right_raw_id)
                if opportunity_id_a is None or opportunity_id_b is None:
                    if pair.confidence_tier == "High":
                        high_pairs_skipped += 1
                    else:
                        medium_pairs_skipped += 1
                    continue

                conn.execute(
                    INSERT_SUSPECTED_DUPLICATE_SQL,
                    (
                        opportunity_id_a,
                        opportunity_id_b,
                        pair.company_name,
                        pair.confidence_tier,
                        pair.matching_fields,
                        pair.matching_field_count,
                    ),
                )
                if pair.confidence_tier == "High":
                    high_pairs_inserted += 1
                else:
                    medium_pairs_inserted += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return SuspectedDuplicatesResult(
        high_pairs_inserted=high_pairs_inserted,
        medium_pairs_inserted=medium_pairs_inserted,
        high_pairs_skipped=high_pairs_skipped,
        medium_pairs_skipped=medium_pairs_skipped,
        expected_high_pairs=duplicate_result.high_confidence_pairs,
        expected_medium_pairs=duplicate_result.medium_confidence_pairs,
    )


def classify_and_route_stage1() -> tuple[Stage1Result, RoutingResult, SuspectedDuplicatesResult]:
    """Classify staged records, detect duplicates, and route to destination tables."""
    with get_database_connection() as conn:
        rows = conn.execute(SELECT_CLASSIFICATION_FIELDS_SQL).fetchall()

    staged_records = [_row_to_record(row) for row in rows]
    classification_result = _build_stage1_result(staged_records)
    duplicate_result = detect_duplicate_suspects(staged_records)
    routing_result = route_stage1_records(staged_records, duplicate_result)
    suspected_duplicates_result = persist_suspected_duplicates(
        duplicate_result.suspected_pairs,
        duplicate_result,
    )

    classification_result.opportunities_inserted = routing_result.opportunities_inserted
    classification_result.exceptions_inserted = routing_result.exceptions_inserted
    classification_result.routing_reconciliation_passed = (
        routing_result.reconciliation_passed
    )
    classification_result.suspected_duplicates_high_inserted = (
        suspected_duplicates_result.high_pairs_inserted
    )
    classification_result.suspected_duplicates_medium_inserted = (
        suspected_duplicates_result.medium_pairs_inserted
    )
    classification_result.suspected_duplicates_high_skipped = (
        suspected_duplicates_result.high_pairs_skipped
    )
    classification_result.suspected_duplicates_medium_skipped = (
        suspected_duplicates_result.medium_pairs_skipped
    )
    classification_result.suspected_duplicates_reconciliation_passed = (
        suspected_duplicates_result.reconciliation_passed
    )
    return classification_result, routing_result, suspected_duplicates_result


def _print_stage1_report(
    result: Stage1Result,
    csv_path: Path = DEFAULT_CSV_PATH,
    imported_count: int | None = None,
) -> None:
    reconciliation_status = "PASS" if result.reconciliation_passed else "FAIL"

    print(f"Database path: {DATABASE_PATH}")
    print(f"CSV path: {csv_path}")
    if imported_count is not None:
        import_reconciliation = (
            "PASS" if imported_count == result.rows_retained_in_staging else "FAIL"
        )
        _print_metric("Import row reconciliation", import_reconciliation)

    _print_section("Stage 1 import summary")
    _print_metric("Total CSV rows read", result.total_rows_checked)
    _print_metric("Rows retained in staging", result.rows_retained_in_staging)
    _print_metric(
        "Fully empty source rows retained in staging but excluded from company classification",
        result.fully_empty_rows,
    )
    _print_metric("Company records evaluated", result.company_records_classified)

    _print_section("Final record classifications")
    _print_metric("Valid", result.valid)
    _print_metric("Incomplete", result.incomplete)
    _print_metric("Ambiguous", result.ambiguous)
    _print_metric("Invalid", result.invalid)
    _print_metric("Companies with all fields valid", result.all_fields_valid)
    _print_metric("Reconciliation", reconciliation_status)

    _print_section("Field-level quality issues")
    _print_metric("Missing company names", result.missing_company_names)
    _print_metric("Missing descriptions", result.missing_descriptions)
    _print_metric("Missing or unknown stages", result.missing_or_unknown_stages)
    _print_metric("Missing geographies", result.missing_geographies)
    _print_metric("Ambiguous geographies", result.ambiguous_geographies)
    _print_metric("Invalid geographies", result.invalid_geographies)
    _print_metric("Missing sectors", result.missing_sectors)
    _print_metric("Ambiguous sectors", result.ambiguous_sectors)
    _print_metric("Invalid sectors", result.invalid_sectors)
    _print_metric("Missing founder backgrounds", result.missing_founder_backgrounds)
    _print_metric("Missing traction values", result.missing_traction_values)
    _print_metric("Missing referral sources", result.missing_referral_sources)
    _print_metric("Invalid referral sources", result.invalid_referral_sources)

    _print_section("Cross-field contamination")
    _print_metric(
        "Invalid stages caused by sector-like values",
        result.invalid_stages_from_sector_contamination,
    )
    _print_metric(
        "Invalid sectors caused by stage-like values",
        result.invalid_sectors_from_stage_contamination,
    )

    _print_section("Record-level issue presence")
    _print_metric(
        "Records with at least one incomplete mandatory field",
        result.records_with_incomplete_mandatory_field,
    )
    _print_metric(
        "Records with at least one ambiguous field",
        result.records_with_ambiguous_field,
    )
    _print_metric(
        "Records with at least one invalid field",
        result.records_with_invalid_field,
    )

    _print_section("Duplicate detection")
    _print_metric("Repeated company-name groups", result.repeated_company_name_groups)
    print()
    # Low-confidence pairs (0-1 matching fields) are computed internally but not
    # reported here: name-only matches are too noisy in this dataset because a
    # small pool of company names is reused across hundreds of records.
    print("Duplicate pairs by confidence tier")
    _print_metric("  High confidence pairs (3 matching fields)", result.high_confidence_pairs)
    _print_metric("  Medium confidence pairs (2 matching fields)", result.medium_confidence_pairs)
    print()
    print("Unique records involved by confidence tier")
    _print_metric("  High confidence", result.unique_records_high_confidence)
    _print_metric("  Medium confidence", result.unique_records_medium_confidence)
    print()
    print("Duplicate-suspected records (High or Medium only, duplicate_suspected=True)")
    _print_metric("  Valid", result.duplicate_suspected_valid)
    _print_metric("  Incomplete", result.duplicate_suspected_incomplete)
    _print_metric("  Ambiguous", result.duplicate_suspected_ambiguous)
    _print_metric("  Invalid", result.duplicate_suspected_invalid)
    _print_metric("  Total duplicate-suspected records", result.duplicate_suspected_count)
    print()
    print(
        "Derived operational queue "
        "(duplicate_suspected=True and validation_status != Invalid)"
    )
    _print_metric("Duplicate Suspected queue", result.duplicate_suspected_queue_count)
    print()

    routing_reconciliation = (
        "PASS" if result.routing_reconciliation_passed else "FAIL"
    )
    _print_section("Stage 1 routing summary")
    _print_metric("Rows inserted into vc_opportunities", result.opportunities_inserted)
    _print_metric("Rows inserted into vc_opportunity_exceptions", result.exceptions_inserted)
    _print_metric(
        "Total rows routed (opportunities + exceptions)",
        result.opportunities_inserted + result.exceptions_inserted,
    )
    _print_metric("Expected total rows", result.total_rows_checked)
    _print_metric("Routing reconciliation", routing_reconciliation)
    print()

    duplicate_reconciliation = (
        "PASS" if result.suspected_duplicates_reconciliation_passed else "FAIL"
    )
    _print_section("Suspected duplicates persistence")
    _print_metric(
        "High confidence pairs inserted into suspected_duplicates",
        result.suspected_duplicates_high_inserted,
    )
    _print_metric(
        "Medium confidence pairs inserted into suspected_duplicates",
        result.suspected_duplicates_medium_inserted,
    )
    _print_metric(
        "Total pairs inserted into suspected_duplicates",
        result.suspected_duplicates_high_inserted
        + result.suspected_duplicates_medium_inserted,
    )
    _print_metric(
        "High confidence pairs skipped (record routed to exceptions)",
        result.suspected_duplicates_high_skipped,
    )
    _print_metric(
        "Medium confidence pairs skipped (record routed to exceptions)",
        result.suspected_duplicates_medium_skipped,
    )
    _print_metric("Expected High confidence pairs", result.high_confidence_pairs)
    _print_metric("Expected Medium confidence pairs", result.medium_confidence_pairs)
    _print_metric("Suspected duplicates reconciliation", duplicate_reconciliation)
    print()


def run_stage1(
    csv_path: str | Path = DEFAULT_CSV_PATH,
    import_if_empty: bool = True,
    force_reimport: bool = False,
) -> Stage1Result:
    """
    Run Stage 1: optional CSV import and record classification.

    When import_if_empty is True, the CSV is imported only if raw_import is empty.
    When force_reimport is True, staging is cleared and the CSV is imported again.
    """
    csv_path = Path(csv_path)
    create_database()

    imported_count: int | None = None
    if force_reimport:
        imported_count = import_csv_force(csv_path)
    elif import_if_empty:
        imported_count = import_csv_if_empty(csv_path)

    result, _routing_result, _suspected_duplicates_result = classify_and_route_stage1()
    _print_stage1_report(result, csv_path=csv_path, imported_count=imported_count)
    return result


if __name__ == "__main__":
    try:
        run_stage1()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"Import blocked: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
