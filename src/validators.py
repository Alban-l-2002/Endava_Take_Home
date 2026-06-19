"""Validation and classification logic for raw import records."""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter, defaultdict
from typing import Any, Literal

import pandas as pd

from src.classification_rules import (
    AMBIGUOUS_GEOGRAPHIES,
    AMBIGUOUS_STAGE_KEYWORDS,
    DESCRIPTION_SIGNAL_KEYWORDS,
    EXPECTED_CSV_FIELDS,
    GEOGRAPHY_NORMALISATION_MAP,
    INCOMPLETE_STAGE_VALUES,
    INVALID_GEOGRAPHIES,
    STAGE_NORMALISATION_MAP,
    VALID_GEOGRAPHIES,
    VALID_REFERRAL_SOURCES,
    VALID_SECTOR_KEYWORDS,
    VALID_STAGE_KEYWORDS,
)

StageClassificationStatus = Literal["Valid", "Incomplete", "Ambiguous", "Invalid"]
SectorClassificationStatus = Literal["Valid", "Incomplete", "Ambiguous", "Invalid"]
FounderBackgroundClassificationStatus = Literal["Valid", "Incomplete"]
TractionClassificationStatus = Literal["Valid", "Incomplete"]
GeographyClassificationStatus = Literal["Valid", "Incomplete", "Ambiguous", "Invalid"]
ReferralClassificationStatus = Literal["Valid", "Incomplete", "Invalid"]
RecordClassificationStatus = Literal["Valid", "Incomplete", "Ambiguous", "Invalid"]

_RECORD_STATUS_PRIORITY: tuple[RecordClassificationStatus, ...] = (
    "Invalid",
    "Incomplete",
    "Ambiguous",
    "Valid",
)

_SECTOR_KEYWORDS_ORDERED = sorted(VALID_SECTOR_KEYWORDS, key=len, reverse=True)

_VALID_REFERRAL_SOURCES_NORMALISED = {
    source.strip().lower() for source in VALID_REFERRAL_SOURCES
}

_VALID_GEOGRAPHIES_NORMALISED = {
    geography.strip().lower() for geography in VALID_GEOGRAPHIES
}
_INVALID_GEOGRAPHIES_NORMALISED = {
    geography.strip().lower() for geography in INVALID_GEOGRAPHIES
}
_AMBIGUOUS_GEOGRAPHIES_NORMALISED = {
    geography.strip().lower() for geography in AMBIGUOUS_GEOGRAPHIES
}


def is_present(value: Any) -> bool:
    """Return True when a field value is non-null and non-blank."""
    if value is None or pd.isna(value):
        return False
    return bool(str(value).strip())


def _normalised_lower(value: Any) -> str:
    return str(value).strip().lower()


def contains_sector_keyword(value: Any) -> bool:
    """Return True when a value contains a recognised sector keyword."""
    lower = _normalised_lower(value)
    return any(keyword in lower for keyword in _SECTOR_KEYWORDS_ORDERED)


def contains_stage_pattern(value: Any) -> bool:
    """Return True when a value contains a recognised stage pattern or keyword."""
    lower = _normalised_lower(value)
    if lower in INCOMPLETE_STAGE_VALUES:
        return True
    if any(keyword in lower for keyword in VALID_STAGE_KEYWORDS):
        return True
    return any(keyword in lower for keyword in AMBIGUOUS_STAGE_KEYWORDS)


def is_fully_empty_row(record: dict[str, Any]) -> bool:
    """Return True when every expected CSV field is blank."""
    return all(not is_present(record.get(field)) for field in EXPECTED_CSV_FIELDS)


def is_stage_incomplete_or_unknown(stage: Any) -> bool:
    """Return True when stage is blank or an explicitly incomplete value."""
    if not is_present(stage):
        return True
    return _normalised_lower(stage) in INCOMPLETE_STAGE_VALUES


def is_referral_invalid(referral_source: Any) -> bool:
    """Return True when referral source is present but not a recognised value."""
    return is_present(referral_source) and classify_referral_source(referral_source) == "Invalid"


def is_geography_ambiguous(geography: Any) -> bool:
    """Return True when geography is present and classified as Ambiguous."""
    return is_present(geography) and classify_geography(geography) == "Ambiguous"


def is_geography_invalid(geography: Any) -> bool:
    """Return True when geography is present and classified as Invalid."""
    return is_present(geography) and classify_geography(geography) == "Invalid"


def is_sector_invalid(sector: Any) -> bool:
    """Return True when sector is present and classified as Invalid."""
    return is_present(sector) and classify_sector(sector) == "Invalid"


def is_sector_ambiguous(sector: Any) -> bool:
    """Return True when sector is present and classified as Ambiguous."""
    return is_present(sector) and classify_sector(sector) == "Ambiguous"


def is_sector_invalid_from_stage_contamination(sector: Any) -> bool:
    """Return True when sector is Invalid because it contains a stage-like value."""
    return is_present(sector) and contains_stage_pattern(sector)


def is_stage_invalid_from_sector_contamination(stage: Any) -> bool:
    """Return True when stage is Invalid because it contains a sector-like value."""
    if not is_present(stage):
        return False
    if _normalised_lower(stage) in INCOMPLETE_STAGE_VALUES:
        return False
    return contains_sector_keyword(stage)


def classify_stage(stage: Any) -> StageClassificationStatus:
    """
    Classify a stage value using rules from classification_rules.py.

    The original stage value is not modified or normalised beyond case-insensitive
    comparison for rule matching.
    """
    if not is_present(stage):
        return "Incomplete"

    lower = _normalised_lower(stage)

    if lower in INCOMPLETE_STAGE_VALUES:
        return "Incomplete"

    if contains_sector_keyword(stage):
        return "Invalid"

    if any(keyword in lower for keyword in VALID_STAGE_KEYWORDS):
        return "Valid"

    if any(keyword in lower for keyword in AMBIGUOUS_STAGE_KEYWORDS):
        return "Ambiguous"

    return "Ambiguous"


def classify_sector(sector: Any) -> SectorClassificationStatus:
    """
    Classify a sector value using sector keywords and stage contamination checks.

    The original sector value is not modified or normalised beyond case-insensitive
    comparison for rule matching.
    """
    if not is_present(sector):
        return "Incomplete"

    if contains_stage_pattern(sector):
        return "Invalid"

    if contains_sector_keyword(sector):
        return "Valid"

    return "Ambiguous"


def classify_founder_background(founder_background: Any) -> FounderBackgroundClassificationStatus:
    """Classify founder background: blank is Incomplete, any non-empty value is Valid."""
    if not is_present(founder_background):
        return "Incomplete"
    return "Valid"


def classify_traction(traction: Any) -> TractionClassificationStatus:
    """Classify traction: blank is Incomplete, any non-empty value is Valid."""
    if not is_present(traction):
        return "Incomplete"
    return "Valid"


def classify_geography(geography: Any) -> GeographyClassificationStatus:
    """
    Classify a geography value using rules from classification_rules.py.

    Matching is exact after trimming whitespace and ignoring case.
    """
    if not is_present(geography):
        return "Incomplete"

    normalized = _normalised_lower(geography)

    if normalized in _INVALID_GEOGRAPHIES_NORMALISED:
        return "Invalid"

    if normalized in _VALID_GEOGRAPHIES_NORMALISED:
        return "Valid"

    if normalized in _AMBIGUOUS_GEOGRAPHIES_NORMALISED:
        return "Ambiguous"

    return "Ambiguous"


def classify_referral_source(referral_source: Any) -> ReferralClassificationStatus:
    """
    Classify a referral source using rules from classification_rules.py.

    Matching is exact after trimming whitespace and ignoring case.
    """
    if not is_present(referral_source):
        return "Incomplete"

    normalized = _normalised_lower(referral_source)
    if normalized in _VALID_REFERRAL_SOURCES_NORMALISED:
        return "Valid"

    return "Invalid"


def _resolve_record_status(
    field_statuses: list[
        StageClassificationStatus
        | SectorClassificationStatus
        | FounderBackgroundClassificationStatus
        | TractionClassificationStatus
        | GeographyClassificationStatus
        | ReferralClassificationStatus
        | RecordClassificationStatus
    ],
) -> RecordClassificationStatus:
    """Apply record-level priority: Invalid, Incomplete, Ambiguous, Valid."""
    status_set = set(field_statuses)
    for status in _RECORD_STATUS_PRIORITY:
        if status in status_set:
            return status
    return "Valid"


def classify_record(record: dict[str, Any]) -> RecordClassificationStatus:
    """
    Classify a non-empty company record using company name, sector, stage,
    founder background, traction, geography, and referral source.

    Description is optional and does not affect the record-level status.
    """
    company_name_status: RecordClassificationStatus = (
        "Incomplete" if not is_present(record.get("company_name")) else "Valid"
    )

    field_statuses = [
        company_name_status,
        classify_sector(record.get("sector")),
        classify_stage(record.get("stage")),
        classify_geography(record.get("geography")),
        classify_founder_background(record.get("founder_background")),
        classify_traction(record.get("traction")),
        classify_referral_source(record.get("referral_source")),
    ]
    return _resolve_record_status(field_statuses)


def is_all_fields_valid(record: dict[str, Any]) -> bool:
    """Return True when every CSV field is present and passes its field-level rules."""
    if is_fully_empty_row(record):
        return False

    return (
        is_present(record.get("company_name"))
        and is_present(record.get("description"))
        and classify_sector(record.get("sector")) == "Valid"
        and classify_stage(record.get("stage")) == "Valid"
        and classify_geography(record.get("geography")) == "Valid"
        and classify_founder_background(record.get("founder_background")) == "Valid"
        and classify_traction(record.get("traction")) == "Valid"
        and classify_referral_source(record.get("referral_source")) == "Valid"
    )


DUPLICATE_COMPARE_FIELDS = ("sector", "stage", "geography")

DuplicateConfidenceTier = Literal["High", "Medium", "Low"]


def normalize_company_name(company_name: Any) -> str:
    """Normalise a company name for duplicate grouping."""
    return str(company_name).strip().lower()


def duplicate_fields_match(left: Any, right: Any) -> bool:
    """Return True when both values are present and equal after trim and case fold."""
    if not is_present(left) or not is_present(right):
        return False
    return str(left).strip().lower() == str(right).strip().lower()


def get_duplicate_matching_field_names(
    left: dict[str, Any],
    right: dict[str, Any],
) -> list[str]:
    """Return sector, stage, and geography fields that match between two records."""
    return [
        field
        for field in DUPLICATE_COMPARE_FIELDS
        if duplicate_fields_match(left.get(field), right.get(field))
    ]


def count_duplicate_matching_fields(
    left: dict[str, Any],
    right: dict[str, Any],
) -> int:
    """Count matching sector, stage, and geography values between two records."""
    return len(get_duplicate_matching_field_names(left, right))


def get_duplicate_pair_confidence(
    left: dict[str, Any],
    right: dict[str, Any],
) -> DuplicateConfidenceTier:
    """
    Return duplicate confidence tier from matching sector, stage, and geography counts.

    3 matching fields -> High, 2 -> Medium, 0 or 1 -> Low.
    """
    match_count = count_duplicate_matching_fields(left, right)
    if match_count >= 3:
        return "High"
    if match_count == 2:
        return "Medium"
    return "Low"


def is_suspected_duplicate_pair(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    """Return True when pair confidence is High or Medium."""
    return get_duplicate_pair_confidence(left, right) in {"High", "Medium"}


@dataclass(frozen=True)
class SuspectedDuplicatePair:
    left_raw_id: Any
    right_raw_id: Any
    company_name: str
    confidence_tier: Literal["High", "Medium"]
    matching_fields: str
    matching_field_count: int


@dataclass
class DuplicateDetectionResult:
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
    high_confidence_record_ids: frozenset[Any]
    medium_confidence_record_ids: frozenset[Any]
    suspected_pairs: tuple[SuspectedDuplicatePair, ...]

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
    def duplicate_suspected_queue_count(self) -> int:
        """Records with duplicate_suspected=True and validation_status != Invalid."""
        return (
            self.duplicate_suspected_valid
            + self.duplicate_suspected_incomplete
            + self.duplicate_suspected_ambiguous
        )


def detect_duplicate_suspects(records: list[dict[str, Any]]) -> DuplicateDetectionResult:
    """
    Detect suspected duplicate records within normalised company-name groups.

    Fully empty rows and records without a company name are excluded.
    """
    eligible_records = [
        record
        for record in records
        if not is_fully_empty_row(record) and is_present(record.get("company_name"))
    ]

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in eligible_records:
        groups[normalize_company_name(record["company_name"])].append(record)

    repeated_company_name_groups = sum(
        1 for group_records in groups.values() if len(group_records) > 1
    )

    high_confidence_pairs = 0
    medium_confidence_pairs = 0
    low_confidence_pairs = 0
    high_tier_record_ids: set[Any] = set()
    medium_tier_record_ids: set[Any] = set()
    low_tier_record_ids: set[Any] = set()
    suspected_pairs: list[SuspectedDuplicatePair] = []

    for group_records in groups.values():
        if len(group_records) < 2:
            continue
        for left_index, left_record in enumerate(group_records):
            for right_record in group_records[left_index + 1 :]:
                confidence = get_duplicate_pair_confidence(left_record, right_record)
                if confidence == "High":
                    high_confidence_pairs += 1
                    high_tier_record_ids.add(left_record["raw_id"])
                    high_tier_record_ids.add(right_record["raw_id"])
                    matching_field_names = get_duplicate_matching_field_names(
                        left_record,
                        right_record,
                    )
                    suspected_pairs.append(
                        SuspectedDuplicatePair(
                            left_raw_id=left_record["raw_id"],
                            right_raw_id=right_record["raw_id"],
                            company_name=left_record["company_name"],
                            confidence_tier="High",
                            matching_fields=",".join(matching_field_names),
                            matching_field_count=len(matching_field_names),
                        )
                    )
                elif confidence == "Medium":
                    medium_confidence_pairs += 1
                    medium_tier_record_ids.add(left_record["raw_id"])
                    medium_tier_record_ids.add(right_record["raw_id"])
                    matching_field_names = get_duplicate_matching_field_names(
                        left_record,
                        right_record,
                    )
                    suspected_pairs.append(
                        SuspectedDuplicatePair(
                            left_raw_id=left_record["raw_id"],
                            right_raw_id=right_record["raw_id"],
                            company_name=left_record["company_name"],
                            confidence_tier="Medium",
                            matching_fields=",".join(matching_field_names),
                            matching_field_count=len(matching_field_names),
                        )
                    )
                else:
                    low_confidence_pairs += 1
                    low_tier_record_ids.add(left_record["raw_id"])
                    low_tier_record_ids.add(right_record["raw_id"])

    duplicate_suspected_ids = high_tier_record_ids | medium_tier_record_ids
    possible_name_collision_ids = low_tier_record_ids - duplicate_suspected_ids

    record_by_id = {record["raw_id"]: record for record in eligible_records}
    duplicate_status_counts: Counter[RecordClassificationStatus] = Counter(
        Valid=0,
        Incomplete=0,
        Ambiguous=0,
        Invalid=0,
    )
    for raw_id in duplicate_suspected_ids:
        validation_status = classify_record(record_by_id[raw_id])
        duplicate_status_counts[validation_status] += 1

    collision_status_counts: Counter[RecordClassificationStatus] = Counter(
        Valid=0,
        Incomplete=0,
        Ambiguous=0,
        Invalid=0,
    )
    for raw_id in possible_name_collision_ids:
        validation_status = classify_record(record_by_id[raw_id])
        collision_status_counts[validation_status] += 1

    return DuplicateDetectionResult(
        repeated_company_name_groups=repeated_company_name_groups,
        high_confidence_pairs=high_confidence_pairs,
        medium_confidence_pairs=medium_confidence_pairs,
        low_confidence_pairs=low_confidence_pairs,
        unique_records_high_confidence=len(high_tier_record_ids),
        unique_records_medium_confidence=len(medium_tier_record_ids),
        unique_records_low_confidence=len(low_tier_record_ids),
        unique_records_duplicate_suspected=len(duplicate_suspected_ids),
        unique_records_possible_name_collision=len(possible_name_collision_ids),
        duplicate_suspected_valid=duplicate_status_counts["Valid"],
        duplicate_suspected_incomplete=duplicate_status_counts["Incomplete"],
        duplicate_suspected_ambiguous=duplicate_status_counts["Ambiguous"],
        duplicate_suspected_invalid=duplicate_status_counts["Invalid"],
        possible_name_collision_valid=collision_status_counts["Valid"],
        possible_name_collision_incomplete=collision_status_counts["Incomplete"],
        possible_name_collision_ambiguous=collision_status_counts["Ambiguous"],
        possible_name_collision_invalid=collision_status_counts["Invalid"],
        high_confidence_record_ids=frozenset(high_tier_record_ids),
        medium_confidence_record_ids=frozenset(medium_tier_record_ids),
        suspected_pairs=tuple(suspected_pairs),
    )


UNRECOVERABLE_MISSING_FIELDS = (
    "founder_background",
    "traction",
    "stage",
    "geography",
)

AMBIGUOUS_REVIEW_FIELDS = ("sector", "stage", "geography")

_FIELD_CLASSIFIERS: dict[str, Any] = {
    "sector": classify_sector,
    "stage": classify_stage,
    "geography": classify_geography,
    "founder_background": classify_founder_background,
    "traction": classify_traction,
    "referral_source": classify_referral_source,
}


def get_duplicate_confidence_tier(
    raw_id: Any,
    duplicate_result: DuplicateDetectionResult,
) -> DuplicateConfidenceTier | None:
    """Return the duplicate confidence tier for a record, if any."""
    if raw_id in duplicate_result.high_confidence_record_ids:
        return "High"
    if raw_id in duplicate_result.medium_confidence_record_ids:
        return "Medium"
    return None


def get_ambiguous_field_names(record: dict[str, Any]) -> list[str]:
    """Return validated fields classified as Ambiguous."""
    return [
        field_name
        for field_name in AMBIGUOUS_REVIEW_FIELDS
        if _FIELD_CLASSIFIERS[field_name](record.get(field_name)) == "Ambiguous"
    ]


def is_sector_only_missing(record: dict[str, Any]) -> bool:
    """Return True when sector is the only incomplete mandatory field."""
    if classify_sector(record.get("sector")) != "Incomplete":
        return False
    if not is_present(record.get("company_name")):
        return False
    if classify_referral_source(record.get("referral_source")) == "Incomplete":
        return False
    for field_name in UNRECOVERABLE_MISSING_FIELDS:
        if _FIELD_CLASSIFIERS[field_name](record.get(field_name)) == "Incomplete":
            return False
    return True


def get_invalid_field_details(record: dict[str, Any]) -> tuple[str, str, Any]:
    """Return exception_reason, affected_field, and affected_value for invalid records."""
    invalid_checks = (
        ("referral_source", "Unrecognised referral source value"),
        ("geography", "Invalid geography value"),
        ("sector", "Stage-like value in sector field"),
        ("stage", "Sector-like value in stage field"),
    )
    for field_name, reason_prefix in invalid_checks:
        value = record.get(field_name)
        if _FIELD_CLASSIFIERS[field_name](value) == "Invalid":
            display_value = value if is_present(value) else None
            if display_value is not None:
                return f"{reason_prefix}: '{display_value}'", field_name, display_value
            return reason_prefix, field_name, display_value
    raise ValueError("Record has no invalid field")


def build_review_assignment(
    record: dict[str, Any],
    duplicate_tier: DuplicateConfidenceTier | None,
) -> tuple[int, str | None, str | None]:
    """Build requires_review, review_reason, and review_priority for vc_opportunities."""
    reasons: list[str] = []

    if duplicate_tier == "High":
        reasons.append("duplicate_suspected_high")
    elif duplicate_tier == "Medium":
        reasons.append("duplicate_suspected_medium")

    for field_name in get_ambiguous_field_names(record):
        reasons.append(f"ambiguous_field:{field_name}")

    for field_name in UNRECOVERABLE_MISSING_FIELDS:
        if _FIELD_CLASSIFIERS[field_name](record.get(field_name)) == "Incomplete":
            reasons.append(f"missing_unrecoverable_field:{field_name}")

    if is_sector_only_missing(record):
        reasons.append("missing_sector_pending_inference")

    if not reasons:
        return 0, None, None

    review_reason = ",".join(reasons)
    has_high_priority_reason = any(
        reason.startswith(
            ("ambiguous_field:", "missing_unrecoverable_field:", "duplicate_suspected_")
        )
        for reason in reasons
    )
    review_priority = "low" if not has_high_priority_reason else "high"
    return 1, review_reason, review_priority


def normalise_stage(raw_value: Any) -> str | None:
    """Return the normalised stage label, or None when unmapped or blank."""
    if not is_present(raw_value):
        return None
    lookup_key = str(raw_value).strip().lower()
    return STAGE_NORMALISATION_MAP.get(lookup_key)


def normalise_geography(raw_value: Any) -> str | None:
    """Return the normalised geography label, or None when unmapped or blank."""
    if not is_present(raw_value):
        return None
    lookup_key = str(raw_value).strip().lower()
    return GEOGRAPHY_NORMALISATION_MAP.get(lookup_key)


def description_has_business_signal(description: Any) -> bool:
    """Return True when description contains at least one business-signal keyword."""
    if not is_present(description):
        return False
    normalised_description = str(description).strip().lower()
    return any(
        keyword in normalised_description
        for keyword in DESCRIPTION_SIGNAL_KEYWORDS
    )
