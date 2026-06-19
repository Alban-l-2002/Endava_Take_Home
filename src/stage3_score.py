"""Stage 3: deterministic and AI-assisted scoring."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from src.classification_rules import (
    SCORING_FOUNDER_POINTS,
    SCORING_GEOGRAPHY_POINTS,
    SCORING_QUALIFYING_GEOGRAPHIES,
    SCORING_QUALIFYING_REFERRAL_SOURCES,
    SCORING_QUALIFYING_SECTORS,
    SCORING_QUALIFYING_STAGES,
    SCORING_REFERRAL_POINTS,
    SCORING_SECTOR_POINTS,
    SCORING_STAGE_POINTS,
    SCORING_TRACTION_POINTS,
)
from src.db_setup import DATABASE_PATH, create_database, get_database_connection
from src.validators import is_present

_LABEL_WIDTH = 86

ScoreDimension = Literal["sector", "geography", "stage", "traction", "founder", "referral"]

# Report order matches scoring matrix presentation.
STAGE3_DIMENSIONS: tuple[ScoreDimension, ...] = (
    "sector",
    "geography",
    "stage",
    "traction",
    "founder",
    "referral",
)

SELECT_OPPORTUNITIES_FOR_SCORING_SQL = """
SELECT
    opportunity_id,
    sector,
    geography,
    stage,
    traction,
    founder_background,
    referral_source
FROM vc_opportunities
ORDER BY opportunity_id
"""

SELECT_SECTOR_INFERRED_OPPORTUNITY_IDS_SQL = """
SELECT DISTINCT opportunity_id
FROM vc_opportunities_normalised
WHERE field_name = 'sector'
  AND method = 'ai_inferred'
"""

DELETE_STAGE3_SCORES_SQL = """
DELETE FROM vc_opportunity_scores
WHERE dimension IN ('sector', 'geography', 'stage', 'traction', 'founder', 'referral')
"""

INSERT_SCORE_SQL = """
INSERT INTO vc_opportunity_scores (
    opportunity_id,
    dimension,
    points_possible,
    points_awarded,
    qualifies,
    based_on_inferred,
    reasoning
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_TRACTION_PROMPT_TEMPLATE = """\
You are evaluating a venture capital traction statement.

Traction: {value}

Does this contain a measurable, named signal of commercial activity?

QUALIFIES if it includes:
- A revenue figure (e.g. £30k MRR, $500k ARR, £25m ARR)
- A customer count (e.g. 12 customers, 50 enterprise clients)
- A pilot count (e.g. 20 enterprise pilots)
- A named financial metric with a concrete referent (e.g. Growing ARR, ARR positive)

DOES NOT QUALIFY if it is vague with no measurable referent:
- e.g. "Growing quickly", "Strong momentum", "Enterprise interest", "Gaining traction"

Reply with exactly one word — either:
QUALIFIES
NO_MATCH"""

_FOUNDER_PROMPT_TEMPLATE = """\
You are evaluating a founder background statement for a venture capital opportunity.

Founder background: {value}

Does this name a specific, verifiable company or institution that implies a credible track record?

QUALIFIES if it names:
- A former employer (e.g. Ex-Stripe, Ex-Google, Former McKinsey consultant, Former SAP product lead)
- A named academic institution (e.g. AI researcher from Cambridge, PhD from MIT)
- A named executive role at a specific organisation

DOES NOT QUALIFY if it is vague or unverifiable:
- e.g. "Repeat founder", "Experienced operator", "Technical founder", "Serial entrepreneur", "Student founders"

Reply with exactly one word — either:
QUALIFIES
NO_MATCH"""


@dataclass
class DimensionScore:
    opportunity_id: int
    dimension: ScoreDimension
    points_possible: int
    points_awarded: int
    qualifies: int
    based_on_inferred: int
    reasoning: str


@dataclass
class DimensionReport:
    dimension: ScoreDimension
    points_possible: int
    excluded_null: int = 0
    qualified: int = 0
    not_qualified: int = 0
    api_calls_made: int = 0
    example_reasoning: list[str] = field(default_factory=list)


@dataclass
class Stage3ScoreResult:
    records_processed: int
    scores_computed: int
    dimension_reports: dict[ScoreDimension, DimensionReport]
    written_to_database: bool = False

    @property
    def scores_inserted(self) -> int:
        return self.scores_computed if self.written_to_database else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case_insensitive_match(value: str, allowed: frozenset[str]) -> bool:
    normalised_allowed = {item.casefold() for item in allowed}
    return value.strip().casefold() in normalised_allowed


def _exact_match(value: str, allowed: frozenset[str]) -> bool:
    return value.strip() in allowed


def _get_anthropic_client() -> Any:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is required for Stage 3 AI scoring "
            "(traction and founder dimensions)."
        )
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _call_claude_binary(prompt: str, *, client: Any) -> bool | None:
    """Call Claude and parse QUALIFIES / NO_MATCH response. Returns None on error."""
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    try:
        message = client.messages.create(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in message.content:
            if block.type == "text":
                text += block.text
        text = text.strip().upper()
        if "QUALIFIES" in text:
            return True
        if "NO_MATCH" in text:
            return False
        return None
    except Exception:
        return None


def _build_ai_scoring_cache(
    unique_values: list[str],
    prompt_template: str,
    dimension_label: str,
    *,
    client: Any,
) -> dict[str, bool | None]:
    """
    Call Claude once per unique value, return a {value: qualifies} cache.
    Logs progress to stdout.
    """
    total = len(unique_values)
    cache: dict[str, bool | None] = {}
    for index, value in enumerate(unique_values, start=1):
        print(f"  {dimension_label} AI scoring: {index}/{total} — {value!r}", flush=True)
        prompt = prompt_template.format(value=value)
        cache[value] = _call_claude_binary(prompt, client=client)
    return cache


# ---------------------------------------------------------------------------
# Deterministic scorers
# ---------------------------------------------------------------------------

def score_sector(
    opportunity_id: int,
    sector: Any,
    *,
    sector_inferred_ids: set[int],
) -> DimensionScore | None:
    if not is_present(sector):
        return None
    sector_value = str(sector).strip()
    qualifies = _case_insensitive_match(sector_value, SCORING_QUALIFYING_SECTORS)
    based_on_inferred = 1 if opportunity_id in sector_inferred_ids else 0
    reasoning = (
        f"Sector '{sector_value}' matches qualifying list"
        if qualifies
        else f"Sector '{sector_value}' not in qualifying list"
    )
    return DimensionScore(
        opportunity_id=opportunity_id,
        dimension="sector",
        points_possible=SCORING_SECTOR_POINTS,
        points_awarded=SCORING_SECTOR_POINTS if qualifies else 0,
        qualifies=1 if qualifies else 0,
        based_on_inferred=based_on_inferred,
        reasoning=reasoning,
    )


def score_geography(opportunity_id: int, geography: Any) -> DimensionScore | None:
    if not is_present(geography):
        return None
    geography_value = str(geography).strip()
    qualifies = _exact_match(geography_value, SCORING_QUALIFYING_GEOGRAPHIES)
    reasoning = (
        f"Geography '{geography_value}' matches qualifying list"
        if qualifies
        else f"Geography '{geography_value}' not in qualifying list"
    )
    return DimensionScore(
        opportunity_id=opportunity_id,
        dimension="geography",
        points_possible=SCORING_GEOGRAPHY_POINTS,
        points_awarded=SCORING_GEOGRAPHY_POINTS if qualifies else 0,
        qualifies=1 if qualifies else 0,
        based_on_inferred=0,
        reasoning=reasoning,
    )


def score_stage(opportunity_id: int, stage: Any) -> DimensionScore | None:
    if not is_present(stage):
        return None
    stage_value = str(stage).strip()
    qualifies = _exact_match(stage_value, SCORING_QUALIFYING_STAGES)
    reasoning = (
        f"Stage '{stage_value}' matches qualifying list"
        if qualifies
        else f"Stage '{stage_value}' not in qualifying list"
    )
    return DimensionScore(
        opportunity_id=opportunity_id,
        dimension="stage",
        points_possible=SCORING_STAGE_POINTS,
        points_awarded=SCORING_STAGE_POINTS if qualifies else 0,
        qualifies=1 if qualifies else 0,
        based_on_inferred=0,
        reasoning=reasoning,
    )


def score_referral(opportunity_id: int, referral_source: Any) -> DimensionScore | None:
    if not is_present(referral_source):
        return None
    referral_value = str(referral_source).strip()
    qualifies = _case_insensitive_match(referral_value, SCORING_QUALIFYING_REFERRAL_SOURCES)
    reasoning = (
        f"Referral '{referral_value}' matches qualifying list"
        if qualifies
        else f"Referral '{referral_value}' not in qualifying list"
    )
    return DimensionScore(
        opportunity_id=opportunity_id,
        dimension="referral",
        points_possible=SCORING_REFERRAL_POINTS,
        points_awarded=SCORING_REFERRAL_POINTS if qualifies else 0,
        qualifies=1 if qualifies else 0,
        based_on_inferred=0,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# AI-assisted scorers
# ---------------------------------------------------------------------------

def score_traction(
    opportunity_id: int,
    traction: Any,
    *,
    cache: dict[str, bool | None],
) -> DimensionScore | None:
    if not is_present(traction):
        return None
    traction_value = str(traction).strip()
    qualifies_result = cache.get(traction_value)

    if qualifies_result is True:
        qualifies = 1
        reasoning = f"Traction '{traction_value}' — measurable signal confirmed"
    elif qualifies_result is False:
        qualifies = 0
        reasoning = f"Traction '{traction_value}' — vague, no measurable signal"
    else:
        # API error or parse failure — treat as not qualifying
        qualifies = 0
        reasoning = f"Traction '{traction_value}' — AI scoring failed, defaulting to 0"

    return DimensionScore(
        opportunity_id=opportunity_id,
        dimension="traction",
        points_possible=SCORING_TRACTION_POINTS,
        points_awarded=SCORING_TRACTION_POINTS if qualifies else 0,
        qualifies=qualifies,
        based_on_inferred=1,
        reasoning=reasoning,
    )


def score_founder(
    opportunity_id: int,
    founder_background: Any,
    *,
    cache: dict[str, bool | None],
) -> DimensionScore | None:
    if not is_present(founder_background):
        return None
    founder_value = str(founder_background).strip()
    qualifies_result = cache.get(founder_value)

    if qualifies_result is True:
        qualifies = 1
        reasoning = f"Founder '{founder_value}' — verifiable named company/institution"
    elif qualifies_result is False:
        qualifies = 0
        reasoning = (
            f"Founder '{founder_value}' — unverifiable claim; "
            "credential cannot be confirmed from available data — routed to analyst review"
        )
    else:
        qualifies = 0
        reasoning = f"Founder '{founder_value}' — AI scoring failed, defaulting to 0"

    return DimensionScore(
        opportunity_id=opportunity_id,
        dimension="founder",
        points_possible=SCORING_FOUNDER_POINTS,
        points_awarded=SCORING_FOUNDER_POINTS if qualifies else 0,
        qualifies=qualifies,
        based_on_inferred=1,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _init_dimension_reports() -> dict[ScoreDimension, DimensionReport]:
    return {
        "sector": DimensionReport(dimension="sector", points_possible=SCORING_SECTOR_POINTS),
        "geography": DimensionReport(dimension="geography", points_possible=SCORING_GEOGRAPHY_POINTS),
        "stage": DimensionReport(dimension="stage", points_possible=SCORING_STAGE_POINTS),
        "traction": DimensionReport(dimension="traction", points_possible=SCORING_TRACTION_POINTS),
        "founder": DimensionReport(dimension="founder", points_possible=SCORING_FOUNDER_POINTS),
        "referral": DimensionReport(dimension="referral", points_possible=SCORING_REFERRAL_POINTS),
    }


def _record_dimension_outcome(
    reports: dict[ScoreDimension, DimensionReport],
    score: DimensionScore | None,
    dimension: ScoreDimension,
) -> DimensionScore | None:
    report = reports[dimension]
    if score is None:
        report.excluded_null += 1
        return None
    if score.qualifies:
        report.qualified += 1
    else:
        report.not_qualified += 1
    if len(report.example_reasoning) < 5:
        report.example_reasoning.append(score.reasoning)
    return score


# ---------------------------------------------------------------------------
# Scoring orchestration
# ---------------------------------------------------------------------------

def run_stage3_scoring() -> tuple[Stage3ScoreResult, list[DimensionScore]]:
    """
    Compute all 6 dimension scores. Calls Claude for traction and founder only.
    Read-only on the database; no INSERT/UPDATE/DELETE.
    """
    create_database()

    with get_database_connection() as conn:
        rows = conn.execute(SELECT_OPPORTUNITIES_FOR_SCORING_SQL).fetchall()
        sector_inferred_ids = {
            row[0]
            for row in conn.execute(SELECT_SECTOR_INFERRED_OPPORTUNITY_IDS_SQL).fetchall()
        }

    total = len(rows)
    print(f"Stage 3: scoring {total} opportunities across 6 dimensions", flush=True)

    # Build deduped AI caches — only unique values are sent to the API.
    unique_traction_values = sorted(
        {str(row[4]).strip() for row in rows if is_present(row[4])},
        key=str.casefold,
    )
    unique_founder_values = sorted(
        {str(row[5]).strip() for row in rows if is_present(row[5])},
        key=str.casefold,
    )

    client = _get_anthropic_client()

    print(
        f"Step 1/2: Traction AI scoring "
        f"({len(unique_traction_values)} unique values → {len(unique_traction_values)} API calls)...",
        flush=True,
    )
    traction_cache = _build_ai_scoring_cache(
        unique_traction_values,
        _TRACTION_PROMPT_TEMPLATE,
        "Traction",
        client=client,
    )

    print(
        f"Step 2/2: Founder AI scoring "
        f"({len(unique_founder_values)} unique values → {len(unique_founder_values)} API calls)...",
        flush=True,
    )
    founder_cache = _build_ai_scoring_cache(
        unique_founder_values,
        _FOUNDER_PROMPT_TEMPLATE,
        "Founder",
        client=client,
    )

    reports = _init_dimension_reports()
    reports["traction"].api_calls_made = len(unique_traction_values)
    reports["founder"].api_calls_made = len(unique_founder_values)

    scores_computed: list[DimensionScore] = []

    for row in rows:
        opportunity_id = row[0]
        sector = row[1]
        geography = row[2]
        stage = row[3]
        traction = row[4]
        founder_background = row[5]
        referral_source = row[6]

        for score in (
            _record_dimension_outcome(
                reports,
                score_sector(opportunity_id, sector, sector_inferred_ids=sector_inferred_ids),
                "sector",
            ),
            _record_dimension_outcome(
                reports,
                score_geography(opportunity_id, geography),
                "geography",
            ),
            _record_dimension_outcome(
                reports,
                score_stage(opportunity_id, stage),
                "stage",
            ),
            _record_dimension_outcome(
                reports,
                score_traction(opportunity_id, traction, cache=traction_cache),
                "traction",
            ),
            _record_dimension_outcome(
                reports,
                score_founder(opportunity_id, founder_background, cache=founder_cache),
                "founder",
            ),
            _record_dimension_outcome(
                reports,
                score_referral(opportunity_id, referral_source),
                "referral",
            ),
        ):
            if score is not None:
                scores_computed.append(score)

    result = Stage3ScoreResult(
        records_processed=total,
        scores_computed=len(scores_computed),
        dimension_reports=reports,
        written_to_database=False,
    )
    return result, scores_computed


def _persist_stage3_scores(conn: Any, scores: list[DimensionScore]) -> None:
    conn.execute(DELETE_STAGE3_SCORES_SQL)
    for score in scores:
        conn.execute(
            INSERT_SCORE_SQL,
            (
                score.opportunity_id,
                score.dimension,
                score.points_possible,
                score.points_awarded,
                score.qualifies,
                score.based_on_inferred,
                score.reasoning,
            ),
        )


def run_stage3_scoring_write() -> Stage3ScoreResult:
    """Compute all 6 dimension scores and persist to vc_opportunity_scores."""
    result, scores_computed = run_stage3_scoring()
    with get_database_connection() as conn:
        try:
            _persist_stage3_scores(conn, scores_computed)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result.written_to_database = True
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_metric(label: str, value: int | str) -> None:
    print(f"{label:<{_LABEL_WIDTH}} {value}")


def _print_section(title: str) -> None:
    print(f"\n## {title}")


def print_stage3_score_report(result: Stage3ScoreResult) -> None:
    """Print Stage 3 scoring report."""
    print(f"\nDatabase path: {DATABASE_PATH}")
    mode = "database write" if result.written_to_database else "dry-run (terminal only)"
    print(f"Mode: {mode}")
    _print_section("Stage 3 scoring summary")
    _print_metric("Total records processed", result.records_processed)
    label = "Score rows inserted (6 dimensions)" if result.written_to_database else "Score rows computed (not written)"
    _print_metric(label, result.scores_computed)

    for dimension in STAGE3_DIMENSIONS:
        report = result.dimension_reports[dimension]
        ai_note = " [AI-scored]" if report.api_calls_made > 0 else ""
        _print_section(f"{dimension.title()} ({report.points_possible} pts){ai_note}")
        if report.api_calls_made > 0:
            _print_metric("Unique values sent to AI", report.api_calls_made)
        _print_metric("Excluded (NULL value)", report.excluded_null)
        _print_metric("Qualified (confident 1)", report.qualified)
        _print_metric("Did not qualify (confident 0)", report.not_qualified)
        print()
        print("Example reasoning strings:")
        if not report.example_reasoning:
            _print_metric("  (none)", 0)
        else:
            for example in report.example_reasoning:
                print(f"  - {example}")
    print()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_stage3(*, write: bool = False) -> Stage3ScoreResult:
    """Run Stage 3 scoring. Writes to DB only when write=True."""
    if write:
        result = run_stage3_scoring_write()
    else:
        result, _scores = run_stage3_scoring()
    print_stage3_score_report(result)
    return result


if __name__ == "__main__":
    write = "--write" in sys.argv
    try:
        run_stage3(write=write)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        sys.exit(1)
