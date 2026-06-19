"""Stage 4: operational outputs and analyst review queues.

Read-only. Produces analyst-ready CSV exports from the data that Stages 1-3 wrote,
plus a terminal recommendation summary. No tables are created or modified here —
outputs are derived views, kept as CSVs so they open directly in Excel / Sheets.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.db_setup import DATABASE_PATH, PROJECT_ROOT, create_database, get_database_connection

OUTPUTS_DIR = PROJECT_ROOT / "outputs"

_LABEL_WIDTH = 40

# Each export: (filename, column headers, SQL). Ordering puts the most
# decision-relevant rows first so an analyst can work top-down.
PRIORITISED_OPPORTUNITIES = (
    "prioritised_opportunities.csv",
    [
        "opportunity_id",
        "company_name",
        "priority_band",
        "total_score",
        "confidence_tier",
        "sector",
        "stage",
        "geography",
        "referral_source",
        "dimensions_scored",
        "score_completeness_pct",
        "requires_review",
        "review_reason",
    ],
    """
    SELECT
        o.opportunity_id,
        o.company_name,
        p.priority_band,
        p.total_score,
        p.confidence_tier,
        o.sector,
        o.stage,
        o.geography,
        o.referral_source,
        p.dimensions_scored,
        p.score_completeness_pct,
        o.requires_review,
        o.review_reason
    FROM vc_opportunities o
    JOIN vc_opportunity_priority p ON o.opportunity_id = p.opportunity_id
    ORDER BY
        CASE p.priority_band
            WHEN 'High' THEN 0
            WHEN 'Medium' THEN 1
            WHEN 'Low' THEN 2
            ELSE 3
        END,
        p.total_score DESC,
        o.opportunity_id
    """,
)

ANALYST_REVIEW_QUEUE = (
    "analyst_review_queue.csv",
    [
        "opportunity_id",
        "company_name",
        "review_priority",
        "review_reason",
        "sector",
        "stage",
        "geography",
        "requires_normalisation_review",
    ],
    """
    SELECT
        opportunity_id,
        company_name,
        review_priority,
        review_reason,
        sector,
        stage,
        geography,
        requires_normalisation_review
    FROM vc_opportunities
    WHERE requires_review = 1
    ORDER BY
        CASE review_priority WHEN 'high' THEN 0 WHEN 'low' THEN 1 ELSE 2 END,
        opportunity_id
    """,
)

DUPLICATE_REVIEW_QUEUE = (
    "duplicate_review_queue.csv",
    [
        "duplicate_id",
        "confidence_tier",
        "company_name",
        "opportunity_id_a",
        "company_a",
        "opportunity_id_b",
        "company_b",
        "matching_fields",
        "matching_field_count",
        "resolution_status",
    ],
    """
    SELECT
        d.duplicate_id,
        d.confidence_tier,
        d.company_name,
        d.opportunity_id_a,
        a.company_name,
        d.opportunity_id_b,
        b.company_name,
        d.matching_fields,
        d.matching_field_count,
        d.resolution_status
    FROM suspected_duplicates d
    JOIN vc_opportunities a ON d.opportunity_id_a = a.opportunity_id
    JOIN vc_opportunities b ON d.opportunity_id_b = b.opportunity_id
    ORDER BY
        CASE d.confidence_tier WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
        d.duplicate_id
    """,
)

IMPORT_EXCEPTIONS = (
    "import_exceptions.csv",
    [
        "exception_id",
        "raw_id",
        "exception_type",
        "exception_reason",
        "affected_field",
        "affected_value",
        "review_status",
    ],
    """
    SELECT
        exception_id,
        raw_id,
        exception_type,
        exception_reason,
        affected_field,
        affected_value,
        review_status
    FROM vc_opportunity_exceptions
    ORDER BY exception_id
    """,
)

ALL_EXPORTS = (
    PRIORITISED_OPPORTUNITIES,
    ANALYST_REVIEW_QUEUE,
    DUPLICATE_REVIEW_QUEUE,
    IMPORT_EXCEPTIONS,
)


@dataclass
class ExportResult:
    filename: str
    row_count: int
    path: Path


@dataclass
class Stage4Result:
    exports: list[ExportResult]
    band_counts: dict[str, int]
    confidence_counts: dict[str, int]
    review_queue_size: int
    duplicate_queue_size: int
    exception_count: int
    opportunities_total: int
    top_opportunities: list[tuple[Any, ...]]


def _write_export(
    conn: Any,
    spec: tuple[str, list[str], str],
) -> ExportResult:
    filename, headers, query = spec
    rows = conn.execute(query).fetchall()
    path = OUTPUTS_DIR / filename
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return ExportResult(filename=filename, row_count=len(rows), path=path)


def _count(conn: Any, query: str) -> int:
    return conn.execute(query).fetchone()[0]


def _band_counts(conn: Any) -> dict[str, int]:
    counts = {"High": 0, "Medium": 0, "Low": 0, "Incomplete": 0}
    for band, count in conn.execute(
        "SELECT priority_band, COUNT(*) FROM vc_opportunity_priority GROUP BY priority_band"
    ).fetchall():
        counts[band] = count
    return counts


def _confidence_counts(conn: Any) -> dict[str, int]:
    counts = {"High": 0, "Medium": 0, "Low": 0}
    for tier, count in conn.execute(
        "SELECT confidence_tier, COUNT(*) FROM vc_opportunity_priority GROUP BY confidence_tier"
    ).fetchall():
        counts[tier] = count
    return counts


def generate_outputs() -> Stage4Result:
    """Generate all Stage 4 CSV exports and gather summary metrics. Read-only."""
    create_database()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    with get_database_connection() as conn:
        exports = [_write_export(conn, spec) for spec in ALL_EXPORTS]

        top_opportunities = conn.execute(
            """
            SELECT
                o.opportunity_id,
                o.company_name,
                p.total_score,
                p.priority_band,
                p.confidence_tier
            FROM vc_opportunities o
            JOIN vc_opportunity_priority p ON o.opportunity_id = p.opportunity_id
            WHERE p.priority_band != 'Incomplete'
            ORDER BY p.total_score DESC, o.opportunity_id
            LIMIT 10
            """
        ).fetchall()

        result = Stage4Result(
            exports=exports,
            band_counts=_band_counts(conn),
            confidence_counts=_confidence_counts(conn),
            review_queue_size=_count(
                conn, "SELECT COUNT(*) FROM vc_opportunities WHERE requires_review = 1"
            ),
            duplicate_queue_size=_count(conn, "SELECT COUNT(*) FROM suspected_duplicates"),
            exception_count=_count(conn, "SELECT COUNT(*) FROM vc_opportunity_exceptions"),
            opportunities_total=_count(conn, "SELECT COUNT(*) FROM vc_opportunities"),
            top_opportunities=top_opportunities,
        )

    return result


def _print_metric(label: str, value: Any) -> None:
    print(f"{label:<{_LABEL_WIDTH}} {value}")


def _print_section(title: str) -> None:
    print(f"\n## {title}")


def print_stage4_report(result: Stage4Result) -> None:
    """Print the Stage 4 recommendation summary (the analyst-facing overview)."""
    print(f"\nDatabase path: {DATABASE_PATH}")
    print(f"Outputs directory: {OUTPUTS_DIR}")

    if not result.exports or all(e.row_count == 0 for e in result.exports[:1]):
        if result.opportunities_total == 0:
            print(
                "\nNo opportunities found. Run Stages 1-3 first "
                "(e.g. `python -m src.main --write-scores`)."
            )

    _print_section("Pipeline summary")
    _print_metric("Opportunities in pipeline", result.opportunities_total)
    _print_metric("Import exceptions (not scored)", result.exception_count)

    _print_section("Priority bands")
    for band in ("High", "Medium", "Low", "Incomplete"):
        _print_metric(f"{band} priority", result.band_counts[band])

    _print_section("Score confidence")
    for tier in ("High", "Medium", "Low"):
        _print_metric(f"{tier} confidence", result.confidence_counts[tier])

    _print_section("Review queues")
    _print_metric("Opportunities flagged for review", result.review_queue_size)
    _print_metric("Suspected duplicate pairs", result.duplicate_queue_size)
    _print_metric("Import exceptions", result.exception_count)

    _print_section("Top 10 opportunities")
    if not result.top_opportunities:
        print("  (none scored — run Stage 3 with --write-scores first)")
    else:
        print(f"  {'opp_id':>6}  {'score':>5}  {'band':<8}  {'conf':<7}  company")
        for opp_id, name, score, band, conf in result.top_opportunities:
            display_name = (name or "(no name)")[:40]
            print(f"  {opp_id:>6}  {score:>5}  {band:<8}  {conf:<7}  {display_name}")

    _print_section("Files written")
    for export in result.exports:
        _print_metric(f"  {export.filename}", f"{export.row_count} rows")
    print()


def run_stage4() -> Stage4Result:
    """Run Stage 4: generate operational outputs and print the summary."""
    result = generate_outputs()
    print_stage4_report(result)
    return result


if __name__ == "__main__":
    try:
        run_stage4()
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
