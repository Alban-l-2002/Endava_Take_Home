"""Configurable classification rules for Stage 1 validation."""

VALID_STAGE_KEYWORDS = {
    "seed",
    "series",
}

AMBIGUOUS_STAGE_KEYWORDS = {
    "growth",
}

INCOMPLETE_STAGE_VALUES = {
    "unknown stage",
}

EXPECTED_CSV_FIELDS = (
    "company_name",
    "description",
    "sector",
    "stage",
    "geography",
    "traction",
    "founder_background",
    "referral_source",
)

MANDATORY_COMPANY_NAME_FIELD = "company_name"

OPTIONAL_DESCRIPTION_FIELD = "description"

SECTOR_FIELD = "sector"

VALID_SECTOR_KEYWORDS = (
    "developer tools",
    "enterprise",
    "security",
    "saas",
    "b2b",
    "tech",
    "ai",
)

STAGE_FIELD = "stage"

REFERRAL_SOURCE_FIELD = "referral_source"

FOUNDER_BACKGROUND_FIELD = "founder_background"

TRACTION_FIELD = "traction"

GEOGRAPHY_FIELD = "geography"

VALID_GEOGRAPHIES = (
    "UK",
    "US",
    "Canada",
    "Netherlands",
    "France",
    "Germany",
    "Spain",
    "England",
    "London",
    "United Kingdom",
)

INVALID_GEOGRAPHIES = (
    "Mars",
)

AMBIGUOUS_GEOGRAPHIES = (
    "EMEA-ish",
)

VALID_REFERRAL_SOURCES = (
    "Event",
    "Website",
    "Cold inbound",
    "Warm intro",
    "Partner referral",
)

# Stage 2 deterministic normalisation maps (keys: trim + lowercase before lookup).
STAGE_NORMALISATION_MAP: dict[str, str | None] = {
    "seed": "Seed",
    "seed stage": "Seed",
    "series seed": "Seed",
    "series a": "Series A",
    "series b": "Series B",
    "pre-seed": "Pre-seed",
    "early growth": "Early Growth",
}

GEOGRAPHY_NORMALISATION_MAP: dict[str, str | None] = {
    "uk": "United Kingdom",
    "england": "United Kingdom",
    "london": "United Kingdom",
    "united kingdom": "United Kingdom",
    "us": "United States",
    "canada": "Canada",
    "netherlands": "Netherlands",
    "france": "France",
    "germany": "Germany",
    "spain": "Spain",
    "emea-ish": None,
}

# Raw geography values excluded from AI fallback (handled as Ambiguous separately).
GEOGRAPHY_AI_EXCLUDED_RAW_VALUES: frozenset[str] = frozenset({"emea-ish"})

# AI-corrected geography whitelist: accepted spellings -> standard normalised name.
GEOGRAPHY_WHITELIST_TO_STANDARD: dict[str, str] = {
    "uk": "United Kingdom",
    "england": "United Kingdom",
    "london": "United Kingdom",
    "united kingdom": "United Kingdom",
    "us": "United States",
    "united states": "United States",
    "canada": "Canada",
    "netherlands": "Netherlands",
    "france": "France",
    "germany": "Germany",
    "spain": "Spain",
}

# Canonical funding stages for AI stage fallback (exact labels only).
STAGE_AI_CANONICAL_OPTIONS: tuple[str, ...] = (
    "Seed",
    "Series A",
    "Series B",
    "Pre-seed",
    "Early Growth",
)

# Stage 2 sector inference — description signal keywords (case-insensitive substring match).
DESCRIPTION_SIGNAL_KEYWORDS: tuple[str, ...] = (
    "platform",
    "software",
    "ai",
    "data",
    "finance",
    "enterprise",
    "customer",
    "business",
    "tool",
    "infrastructure",
    "automation",
    "analytics",
    "saas",
    "b2b",
    "solution",
    "health",
    "legal",
    "hr",
    "payment",
    "security",
    "supply chain",
    "developer",
)

# Fixed sector list for Claude API inference (exact labels only).
SECTOR_INFERENCE_OPTIONS: tuple[str, ...] = (
    "B2B SaaS",
    "Enterprise AI",
    "FinTech SaaS",
    "HealthTech B2B",
    "Supply Chain SaaS",
    "Developer Tools",
    "Cybersecurity",
    "LegalTech",
    "HR Tech",
)

# Stage 3 scoring — qualifying values (deterministic dimensions).
SCORING_SECTOR_POINTS = 25
SCORING_TRACTION_POINTS = 20
SCORING_GEOGRAPHY_POINTS = 15
SCORING_STAGE_POINTS = 15
SCORING_FOUNDER_POINTS = 15
SCORING_REFERRAL_POINTS = 10

SCORING_QUALIFYING_SECTORS: frozenset[str] = frozenset(SECTOR_INFERENCE_OPTIONS)

SCORING_QUALIFYING_GEOGRAPHIES: frozenset[str] = frozenset(
    {
        "United Kingdom",
        "France",
        "Germany",
        "Spain",
        "Netherlands",
        "United States",
        "Canada",
    }
)

SCORING_QUALIFYING_STAGES: frozenset[str] = frozenset({"Seed", "Series A"})

SCORING_QUALIFYING_REFERRAL_SOURCES: frozenset[str] = frozenset(
    {"Warm intro", "Partner referral"}
)

# Stage 3 priority aggregation.
# Total possible across all 6 dimensions.
SCORING_DIMENSIONS_POSSIBLE = 6

# Mandatory thesis fields. If any is missing after normalisation, the opportunity
# cannot be fairly scored against the investment thesis and is marked Incomplete
# (rather than given an artificially low score). Strength signals (traction,
# founder, referral) are NOT mandatory — their absence simply scores 0 points.
SCORING_MANDATORY_FIELDS: tuple[str, ...] = ("sector", "geography", "stage")

# Priority band thresholds (inclusive lower bounds), applied to the total score.
PRIORITY_HIGH_THRESHOLD = 75
PRIORITY_MEDIUM_THRESHOLD = 50
