# VC Pipeline — Database ER Diagram

Entity-relationship view of the SQLite schema defined in [`src/db_setup.py`](../src/db_setup.py)
(`CREATE_TABLES_SQL`). Relationships and key columns reflect the live database
(`vc_pipeline.db`). Row counts are from the current build.

> `PRAGMA foreign_keys = ON` is set on every connection
> (`get_database_connection`), so these relationships are enforced, not just documented.

---

## 1. Relationships overview

```mermaid
erDiagram
    raw_import                  ||--o| vc_opportunities            : "raw_id (UNIQUE) — 1:0..1"
    raw_import                  ||--o{ vc_opportunity_exceptions   : "raw_id — rejected rows"
    vc_opportunities            ||--o{ vc_opportunities_normalised : "opportunity_id — audit trail"
    vc_opportunities            ||--o{ vc_opportunity_scores       : "opportunity_id — 1 row / dimension"
    vc_opportunities            ||--|| vc_opportunity_priority     : "opportunity_id (PK) — 1:1 verdict"
    vc_opportunities            ||--o{ suspected_duplicates        : "opportunity_id_a"
    vc_opportunities            ||--o{ suspected_duplicates        : "opportunity_id_b"
```

**Reading the crow's-feet:**

| Pair | Cardinality | Meaning |
|------|-------------|---------|
| `raw_import` → `vc_opportunities` | 1 : 0-or-1 | each staged row becomes **at most one** opportunity (`raw_id` is `UNIQUE`); rejected rows become none |
| `raw_import` → `vc_opportunity_exceptions` | 1 : many | a rejected/empty row is logged as one (or more) exceptions |
| `vc_opportunities` → `vc_opportunities_normalised` | 1 : many | one audit row per field transformed |
| `vc_opportunities` → `vc_opportunity_scores` | 1 : many | one row per scored dimension (NULL fields excluded) |
| `vc_opportunities` → `vc_opportunity_priority` | 1 : 1 | exactly one ranked recommendation (`opportunity_id` is the PK) |
| `vc_opportunities` → `suspected_duplicates` | 1 : many | a record can appear in many flagged pairs (as side A or side B) |

---

## 2. Full schema with columns

```mermaid
erDiagram
    raw_import {
        INTEGER raw_id PK "AUTOINCREMENT"
        INTEGER source_row_number "physical CSV line"
        TEXT    company_name
        TEXT    description
        TEXT    sector
        TEXT    stage
        TEXT    geography
        TEXT    traction
        TEXT    founder_background
        TEXT    referral_source
        TEXT    processing_status "Pending|Accepted|Exception|Failed"
        TEXT    imported_at
    }

    vc_opportunities {
        INTEGER opportunity_id PK
        INTEGER raw_id FK "UNIQUE -> raw_import"
        TEXT    company_name
        TEXT    sector "normalised"
        TEXT    stage "normalised"
        TEXT    geography "normalised"
        TEXT    traction
        TEXT    founder_background
        TEXT    referral_source
        TEXT    validation_status "Valid|Incomplete|Ambiguous|Duplicate Suspected"
        TEXT    validation_reason
        INTEGER requires_review "0|1"
        INTEGER requires_normalisation_review "0|1"
        INTEGER duplicate_suspected "0|1"
        TEXT    review_reason
        TEXT    review_priority "high|low"
        TEXT    created_at
    }

    vc_opportunity_exceptions {
        INTEGER exception_id PK
        INTEGER raw_id FK "-> raw_import"
        TEXT    exception_type "Incomplete|Ambiguous|Invalid|Malformed|Duplicate Suspected"
        TEXT    exception_reason
        TEXT    affected_field
        TEXT    affected_value
        TEXT    review_status "Open|Reviewed|Resolved|Rejected"
        TEXT    analyst_notes
        TEXT    created_at
    }

    suspected_duplicates {
        INTEGER duplicate_id PK
        INTEGER opportunity_id_a FK "-> vc_opportunities"
        INTEGER opportunity_id_b FK "-> vc_opportunities"
        TEXT    company_name
        TEXT    confidence_tier "High|Medium"
        TEXT    matching_fields
        INTEGER matching_field_count
        TEXT    resolution_status "Pending|Confirmed Duplicate|Different Company"
        TEXT    analyst_notes
        TEXT    created_at
    }

    vc_opportunities_normalised {
        INTEGER normalisation_id PK
        INTEGER opportunity_id FK "-> vc_opportunities"
        TEXT    field_name "stage|geography|sector"
        TEXT    original_value "preserved source value"
        TEXT    method "deterministic|ai_fallback|ai_inferred"
        TEXT    confidence "High|Medium"
        TEXT    created_at
    }

    vc_opportunity_scores {
        INTEGER score_id PK
        INTEGER opportunity_id FK "-> vc_opportunities"
        TEXT    dimension "sector|geography|stage|traction|founder|referral"
        INTEGER points_possible
        INTEGER points_awarded
        INTEGER qualifies "0|1"
        INTEGER based_on_inferred "0|1 — AI provenance"
        TEXT    reasoning
        TEXT    created_at
    }

    vc_opportunity_priority {
        INTEGER opportunity_id PK "also FK -> vc_opportunities (1:1)"
        INTEGER total_score
        TEXT    priority_band "High|Medium|Low|Incomplete"
        TEXT    confidence_tier "High|Medium|Low"
        INTEGER dimensions_scored
        INTEGER dimensions_possible "default 6"
        REAL    score_completeness_pct
        TEXT    scored_at
    }

    raw_import                  ||--o| vc_opportunities            : raw_id
    raw_import                  ||--o{ vc_opportunity_exceptions   : raw_id
    vc_opportunities            ||--o{ vc_opportunities_normalised : opportunity_id
    vc_opportunities            ||--o{ vc_opportunity_scores       : opportunity_id
    vc_opportunities            ||--|| vc_opportunity_priority     : opportunity_id
    vc_opportunities            ||--o{ suspected_duplicates        : opportunity_id_a
    vc_opportunities            ||--o{ suspected_duplicates        : opportunity_id_b
```

---

## 3. Live row counts

| Table | Rows | Grain |
|-------|-----:|-------|
| `raw_import` | 495 | 1 per CSV row (immutable staging) |
| `vc_opportunities` | 480 | 1 per usable record |
| `vc_opportunity_exceptions` | 15 | 1 per rejected row + reason |
| `suspected_duplicates` | 32 | 1 per flagged pair |
| `vc_opportunities_normalised` | 463 | 1 per field transformation |
| `vc_opportunity_scores` | 2,709 | 1 per opportunity **per dimension** |
| `vc_opportunity_priority` | 480 | 1 per opportunity (final verdict) |

Reconciliation: **495 staged = 480 opportunities + 15 exceptions** (nothing lost);
**480 opportunities = 480 priority rows** (1:1).

---

## 4. The trace-back path (lineage in two joins)

Any final recommendation traces back to the original CSV line:

```mermaid
flowchart LR
    P["vc_opportunity_priority<br/>opportunity_id, priority_band"]
    O["vc_opportunities<br/>opportunity_id, raw_id (UNIQUE)"]
    R["raw_import<br/>raw_id, source_row_number"]
    CSV["CSV line<br/>(source_row_number)"]

    P -->|"opportunity_id"| O
    O -->|"raw_id"| R
    R -->|"source_row_number"| CSV
```

```sql
SELECT r.source_row_number, r.company_name, p.priority_band, p.total_score
FROM vc_opportunity_priority p
JOIN vc_opportunities o ON o.opportunity_id = p.opportunity_id
JOIN raw_import        r ON r.raw_id        = o.raw_id
WHERE p.opportunity_id = 1;
```

**Indexing note (honest):** the only secondary index is the auto-index from
`vc_opportunities.raw_id UNIQUE`; the `opportunity_id` foreign keys on `scores`,
`normalised`, and `exceptions` are unindexed. Irrelevant at 500 rows (full scans are
instant); the first thing to index when scaling to millions, where production storage
would be Dataverse anyway.
```
