"""SQLite database setup for the VC pipeline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATABASE_PATH = PROJECT_ROOT / "vc_pipeline.db"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS raw_import (
    raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_row_number INTEGER NOT NULL,
    company_name TEXT,
    description TEXT,
    sector TEXT,
    stage TEXT,
    geography TEXT,
    traction TEXT,
    founder_background TEXT,
    referral_source TEXT,
    processing_status TEXT NOT NULL DEFAULT 'Pending'
        CHECK (processing_status IN ('Pending', 'Accepted', 'Exception', 'Failed')),
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vc_opportunities (
    opportunity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id INTEGER NOT NULL UNIQUE,
    company_name TEXT,
    description TEXT,
    traction TEXT,
    founder_background TEXT,
    referral_source TEXT,
    stage TEXT,
    geography TEXT,
    sector TEXT,
    requires_normalisation_review INTEGER NOT NULL DEFAULT 0
        CHECK (requires_normalisation_review IN (0, 1)),
    validation_status TEXT NOT NULL
        CHECK (validation_status IN ('Valid', 'Incomplete', 'Ambiguous', 'Duplicate Suspected')),
    validation_reason TEXT,
    requires_review INTEGER NOT NULL DEFAULT 0 CHECK (requires_review IN (0, 1)),
    duplicate_suspected INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_suspected IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_reason TEXT,
    review_priority TEXT CHECK (review_priority IN ('high', 'low')),
    FOREIGN KEY (raw_id) REFERENCES raw_import (raw_id)
);

CREATE TABLE IF NOT EXISTS vc_opportunity_exceptions (
    exception_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id INTEGER NOT NULL,
    exception_type TEXT NOT NULL
        CHECK (exception_type IN (
            'Incomplete', 'Ambiguous', 'Invalid', 'Malformed', 'Duplicate Suspected'
        )),
    exception_reason TEXT NOT NULL,
    affected_field TEXT,
    affected_value TEXT,
    review_status TEXT NOT NULL DEFAULT 'Open'
        CHECK (review_status IN ('Open', 'Reviewed', 'Resolved', 'Rejected')),
    analyst_notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (raw_id) REFERENCES raw_import (raw_id)
);

CREATE TABLE IF NOT EXISTS suspected_duplicates (
    duplicate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id_a INTEGER NOT NULL,
    opportunity_id_b INTEGER NOT NULL,
    company_name TEXT NOT NULL,
    confidence_tier TEXT NOT NULL
        CHECK (confidence_tier IN ('High', 'Medium')),
    matching_fields TEXT NOT NULL,
    matching_field_count INTEGER NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'Pending'
        CHECK (resolution_status IN (
            'Pending', 'Confirmed Duplicate', 'Different Company'
        )),
    analyst_notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id_a) REFERENCES vc_opportunities (opportunity_id),
    FOREIGN KEY (opportunity_id_b) REFERENCES vc_opportunities (opportunity_id)
);

CREATE TABLE IF NOT EXISTS vc_opportunities_normalised (
    normalisation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    field_name TEXT NOT NULL
        CHECK (field_name IN ('stage', 'geography', 'sector')),
    original_value TEXT,
    method TEXT NOT NULL
        CHECK (method IN ('deterministic', 'ai_fallback', 'ai_inferred')),
    confidence TEXT CHECK (confidence IN ('High', 'Medium')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES vc_opportunities (opportunity_id)
);

CREATE TABLE IF NOT EXISTS vc_opportunity_scores (
    score_id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    dimension TEXT NOT NULL
        CHECK (dimension IN ('sector', 'geography', 'stage', 'traction', 'founder', 'referral')),
    points_possible INTEGER NOT NULL,
    points_awarded INTEGER NOT NULL,
    qualifies INTEGER NOT NULL CHECK (qualifies IN (0, 1)),
    based_on_inferred INTEGER NOT NULL DEFAULT 0 CHECK (based_on_inferred IN (0, 1)),
    reasoning TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES vc_opportunities (opportunity_id)
);

CREATE TABLE IF NOT EXISTS vc_opportunity_priority (
    opportunity_id INTEGER PRIMARY KEY,
    total_score INTEGER NOT NULL,
    priority_band TEXT NOT NULL
        CHECK (priority_band IN ('High', 'Medium', 'Low', 'Incomplete')),
    confidence_tier TEXT NOT NULL DEFAULT 'High'
        CHECK (confidence_tier IN ('High', 'Medium', 'Low')),
    dimensions_scored INTEGER NOT NULL,
    dimensions_possible INTEGER NOT NULL DEFAULT 6,
    score_completeness_pct REAL,
    scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES vc_opportunities (opportunity_id)
);
"""

# Additive, idempotent column migrations: {table_name: {column_name: column_definition}}.
# Lets the schema evolve without a destructive rebuild (and without losing existing rows).
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "vc_opportunity_priority": {
        "confidence_tier": "TEXT NOT NULL DEFAULT 'High'",
    },
}

REBUILD_PIPELINE_TABLES_SQL = """
DROP TABLE IF EXISTS vc_opportunity_priority;
DROP TABLE IF EXISTS vc_opportunity_scores;
DROP TABLE IF EXISTS vc_opportunities_normalised;
DROP TABLE IF EXISTS suspected_duplicates;
DROP TABLE IF EXISTS vc_opportunity_exceptions;
DROP TABLE IF EXISTS vc_opportunities;
"""


def get_database_connection() -> sqlite3.Connection:
    """Return a SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """Add any missing columns to existing tables (safe, additive, idempotent)."""
    for table, columns in _COLUMN_MIGRATIONS.items():
        present = _existing_columns(conn, table)
        for column, definition in columns.items():
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def create_database() -> None:
    """Create pipeline tables if they do not already exist, then apply migrations."""
    with get_database_connection() as conn:
        conn.executescript(CREATE_TABLES_SQL)
        _apply_column_migrations(conn)
        conn.commit()


def rebuild_pipeline_schema(*, clear_staging: bool = True) -> None:
    """Drop and recreate downstream pipeline tables with the current schema."""
    with get_database_connection() as conn:
        conn.executescript(REBUILD_PIPELINE_TABLES_SQL)
        conn.executescript(CREATE_TABLES_SQL)
        if clear_staging:
            conn.execute("DELETE FROM raw_import")
        conn.commit()
