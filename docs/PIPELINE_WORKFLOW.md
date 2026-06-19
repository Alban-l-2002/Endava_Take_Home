# VC Pipeline — Workflow, Data Flow & Decision Logic

Deliverable artefact for the Copilot Studio Automation Challenge.  
Shows how the prototype handles **import → validation → routing → normalisation → scoring → exceptions → analyst review**.

> GitHub renders Mermaid diagrams in any `.md` file — this document and the README look the same when viewed on GitHub.

---

## 1. End-to-end data flow

High-level view of how a CSV row moves through the pipeline and which tables it lands in.

```mermaid
flowchart LR
    CSV["CSV file\n(data/vc_opportunities_dataset.csv)"]

    subgraph S1["Stage 1 — Import & classify"]
        RI["raw_import\n(exact source copy)"]
        ROUTE{"Route"}
    end

    subgraph S2["Stage 2 — Normalise"]
        NORM["Deterministic maps\n→ AI fallback\n→ AI sector inference"]
        AUDIT["vc_opportunities_normalised\n(audit trail)"]
    end

    subgraph S3["Stage 3 — Score"]
        SCORES["vc_opportunity_scores\n(6 dimensions)"]
        PRIORITY["vc_opportunity_priority\n(planned)"]
    end

    subgraph REVIEW["Analyst review (throughout)"]
        EXC["vc_opportunity_exceptions"]
        DUP["suspected_duplicates"]
        FLAGS["vc_opportunities\nrequires_review flags"]
    end

    subgraph S4["Stage 4 — Outputs (planned)"]
        OUT["Review queues\n& operational exports"]
    end

    CSV --> RI
    RI --> ROUTE
    ROUTE -->|"Invalid / empty row"| EXC
    ROUTE -->|"Valid / Incomplete / Ambiguous"| VO["vc_opportunities"]
    ROUTE --> DUP

    VO --> NORM
    NORM --> AUDIT
    NORM --> VO

    VO --> SCORES
    SCORES --> PRIORITY
    SCORES --> OUT
    FLAGS --> OUT
    EXC --> OUT
    DUP --> OUT
```

**Principle:** `raw_import` is never modified after staging. Every downstream decision can be traced back to the original CSV row via `raw_id` and `source_row_number`.

---

## 2. Stage 1 — Import, validation & routing

Each staged row is classified, then routed. Invalid records never enter the scoring pipeline.

```mermaid
flowchart TD
    START(["CSV row staged in raw_import"])

    EMPTY{"Fully empty row?"}
    CLASSIFY["classify_record()\nper-field rules"]

    STATUS{"validation_status?"}

    INVALID["Insert vc_opportunity_exceptions\nexception_type = Invalid\nNever scored"]
    OPPORTUNITY["Insert vc_opportunities\nvalidation_status preserved"]

    DUP["detect_duplicate_suspects()\nHigh / Medium pairs → suspected_duplicates"]

    REVIEW{"build_review_assignment()"}

    NO_FLAG["requires_review = 0"]
    FLAG["requires_review = 1\nreview_reason + review_priority"]

    START --> EMPTY
    EMPTY -->|Yes| INVALID
    EMPTY -->|No| CLASSIFY
    CLASSIFY --> STATUS

    STATUS -->|Invalid| INVALID
    STATUS -->|"Valid / Incomplete / Ambiguous"| OPPORTUNITY

    OPPORTUNITY --> DUP
    DUP --> REVIEW

    REVIEW -->|"No review triggers"| NO_FLAG
    REVIEW -->|"One or more triggers"| FLAG
```

### Per-field classification outcomes

| Outcome | Meaning | Example |
|---------|---------|---------|
| **Valid** | Value is present and recognised | `referral_source = Warm intro` |
| **Incomplete** | Mandatory field missing | Blank `traction` |
| **Ambiguous** | Present but not mappable | `geography = EMEA-ish` |
| **Invalid** | Structurally unusable | `geography = Mars`, column contamination |

### Routing rules

| Condition | Destination | Scored? |
|-----------|-------------|---------|
| Fully empty row | `vc_opportunity_exceptions` | No |
| `validation_status = Invalid` | `vc_opportunity_exceptions` | No |
| `Valid`, `Incomplete`, or `Ambiguous` | `vc_opportunities` | Yes (if structurally usable) |

---

## 3. Analyst review — decision tree

Review flags are set at Stage 1 routing. They do **not** block scoring — they queue the record for human attention alongside its score.

```mermaid
flowchart TD
    REC(["Record routed to vc_opportunities"])

    R1{"Duplicate suspected?\nHigh or Medium confidence"}
    R2{"Ambiguous field?\ne.g. EMEA-ish geography"}
    R3{"Unrecoverable missing field?\nfounder / traction / stage / geography"}
    R4{"Sector only missing?\nAll other mandatory fields present"}

    HIGH["review_priority = high"]
    LOW["review_priority = low"]
    CLEAR["requires_review = 0"]

    REC --> R1
    R1 -->|Yes| HIGH
    R1 -->|No| R2
    R2 -->|Yes| HIGH
    R2 -->|No| R3
    R3 -->|Yes| HIGH
    R3 -->|No| R4
    R4 -->|Yes| LOW
    R4 -->|No| CLEAR

    HIGH --> REASONS["review_reason examples:\n• duplicate_suspected_high\n• ambiguous_field:geography\n• missing_unrecoverable_field:traction"]
    LOW --> REASONS2["review_reason example:\n• missing_sector_pending_inference"]
```

**Separate review queues (not auto-resolved):**

| Queue | Table | Trigger |
|-------|-------|---------|
| Exceptions | `vc_opportunity_exceptions` | Invalid or empty rows |
| Duplicate pairs | `suspected_duplicates` | High/Medium confidence name matches |
| Opportunity flags | `vc_opportunities.requires_review` | Ambiguous, missing, or duplicate flags |
| Normalisation audit | `vc_opportunities_normalised` + `requires_normalisation_review` | Any AI or rule-based transformation |

---

## 4. Stage 2 — Normalisation flow

Stage and geography use deterministic rules first; AI is a fallback only. Sector uses AI inference only when missing.

```mermaid
flowchart TD
    ROW(["vc_opportunities row\n(read raw values via raw_id join)"])

    subgraph STAGE["stage field"]
        S1{"Deterministic\nnormalise_stage()"}
        S2{"Blank?"}
        S3{"AI fallback eligible?"}
        S4["Claude: spelling/format\ncorrection only"]
        SNULL["Leave NULL"]
        SOK["Write canonical stage"]
    end

    subgraph GEO["geography field"]
        G1{"Deterministic\nnormalise_geography()"}
        G2{"Blank or EMEA-ish?"}
        G3{"AI fallback eligible?"}
        G4["Claude: country spelling\ncorrection only"]
        GNULL["Leave NULL"]
        GOK["Write canonical geography"]
    end

    subgraph SEC["sector field"]
        SEC1{"Sector present?"}
        SEC2{"Description has\nbusiness signal?"}
        SEC3["Claude: infer sector\nfrom description only"]
        SECNULL["Leave NULL"]
        SECOK["Write sector value"]
    end

    AUDIT["If value changed:\ninsert vc_opportunities_normalised\n(method + confidence)\nSet requires_normalisation_review = 1"]

    ROW --> S1
    S1 -->|Match| SOK
    S1 -->|No match| S2
    S2 -->|Yes| SNULL
    S2 -->|No| S3
    S3 -->|Yes| S4
    S3 -->|No| SNULL
    S4 -->|Corrected| SOK
    S4 -->|NO_MATCH| SNULL

    ROW --> G1
    G1 -->|Match| GOK
    G1 -->|No match| G2
    G2 -->|Yes| GNULL
    G2 -->|No| G3
    G3 -->|Yes| G4
    G3 -->|No| GNULL
    G4 -->|Corrected| GOK
    G4 -->|NO_MATCH| GNULL

    ROW --> SEC1
    SEC1 -->|Yes| SECOK
    SEC1 -->|No| SEC2
    SEC2 -->|No signal| SECNULL
    SEC2 -->|Signal| SEC3
    SEC3 -->|Match| SECOK
    SEC3 -->|NO_MATCH| SECNULL

    SOK --> AUDIT
    GOK --> AUDIT
    SECOK --> AUDIT
```

**Deterministic vs AI:**

| Step | Type | When used |
|------|------|-----------|
| Lookup maps | Deterministic | Known values (e.g. `uk` → `United Kingdom`, `seed` → `Seed`) |
| AI fallback | AI (Medium confidence) | Stage/geography typo correction into known categories only |
| AI inference | AI (High/Medium/Low) | Missing sector only, after description signal check |

---

## 5. Stage 3 — Scoring logic

Each dimension is boolean: full points or zero. NULL fields are **excluded** (no row written for that dimension).

```mermaid
flowchart TD
    OPP(["vc_opportunities row"])

    subgraph DET["Deterministic scoring"]
        D1["Sector (25 pts)\nQualifying B2B/enterprise list"]
        D2["Geography (15 pts)\nEurope or North America"]
        D3["Stage (15 pts)\nSeed or Series A only"]
        D4["Referral (10 pts)\nWarm intro or Partner referral"]
    end

    subgraph AI["AI-assisted scoring"]
        D5["Traction (20 pts)\nMeasurable commercial signal?"]
        D6["Founder (15 pts)\nNamed verifiable credential?"]
    end

    NULL{"Field NULL?"}
    EXCLUDE["Exclude dimension\n(no score row)"]
    WRITE["Write vc_opportunity_scores\nqualifies = 0 or 1\nreasoning + based_on_inferred flag"]

    OPP --> NULL
    NULL -->|Yes| EXCLUDE
    NULL -->|No| D1 & D2 & D3 & D4 & D5 & D6
    D1 & D2 & D3 & D4 & D5 & D6 --> WRITE

    WRITE --> SUM["Sum points_awarded per opportunity\n→ vc_opportunity_priority\n(band + confidence_tier)"]
```

### Scoring matrix

| Dimension | Points | Qualifies when | Method |
|-----------|--------|----------------|--------|
| Sector | 25 | B2B SaaS, Enterprise AI, FinTech SaaS, HealthTech B2B, Supply Chain SaaS, Developer Tools, Cybersecurity, LegalTech, HR Tech | Deterministic |
| Geography | 15 | United Kingdom, France, Germany, Spain, Netherlands, United States, Canada | Deterministic |
| Stage | 15 | Seed or Series A (exact) | Deterministic |
| Traction | 20 | Revenue figure, customer count, pilot count, or named metric (e.g. Growing ARR) | AI |
| Founder | 15 | Named company or institution (e.g. Ex-Stripe, Cambridge) | AI |
| Referral | 10 | Warm intro or Partner referral | Deterministic |

### Priority bands

Each opportunity's awarded points are summed into a single recommendation in `vc_opportunity_priority`.

| Total score | Priority band |
|-------------|---------------|
| 75+ | High |
| 50–74 | Medium |
| Below 50 | Low |
| Missing a mandatory field (`sector`, `geography`, or `stage`) | Incomplete |

**Mandatory fields:** an opportunity is marked **Incomplete** (not scored into a band) if `sector`, `geography`, or `stage` is missing after normalisation — these are the structural thesis fields, and without them the deal can't be fairly assessed. Strength signals (traction, founder, referral) simply score 0 when absent rather than forcing Incomplete.

### Confidence tier

Alongside the band, each priority row carries a `confidence_tier` that rates how much to trust the recommendation:

| Tier | Meaning |
|------|---------|
| High | Fully deterministic, clean data — no review flags |
| Medium | A value was AI-corrected during normalisation, but no review is outstanding |
| Low | Flagged for analyst review (missing/ambiguous data, suspected duplicate, or sector pending inference) — recommendation is provisional |

---

## 6. Scoring pseudo-code

Condensed logic as implemented in `stage3_score.py` and `validators.py`.

```
FOR EACH row IN raw_import:

  # --- Stage 1 ---
  IF row is fully empty:
      INSERT vc_opportunity_exceptions (type=Invalid)
      CONTINUE

  status = classify_record(row)   # per-field: Valid | Incomplete | Ambiguous | Invalid

  IF status == Invalid:
      INSERT vc_opportunity_exceptions (type=Invalid, reason, affected_field)
      CONTINUE

  INSERT vc_opportunities (status, review flags from build_review_assignment())
  IF duplicate pair High/Medium: INSERT suspected_duplicates


FOR EACH opportunity IN vc_opportunities:

  # --- Stage 2 ---
  FOR field IN (stage, geography, sector):
      value = resolve(field)          # deterministic → AI fallback → AI inference
      IF value changed from raw:
          INSERT vc_opportunities_normalised (original, method, confidence)
          SET requires_normalisation_review = 1
      UPDATE vc_opportunities.field = value


FOR EACH opportunity IN vc_opportunities:

  # --- Stage 3 ---
  FOR dimension IN (sector, geography, stage, traction, founder, referral):
      IF opportunity.field IS NULL:
          SKIP dimension                    # excluded, not penalised
      ELSE IF dimension qualifies:
          INSERT vc_opportunity_scores (points_awarded = points_possible, qualifies = 1)
      ELSE:
          INSERT vc_opportunity_scores (points_awarded = 0, qualifies = 0)

  # --- Stage 3: priority aggregation ---
  total = SUM(points_awarded)
  band  = priority_band(total, mandatory_fields_present)   # High/Medium/Low/Incomplete
  conf  = confidence_tier(requires_review, requires_normalisation_review)
  INSERT vc_opportunity_priority (total, band, conf, dimensions_scored, completeness)


# --- Stage 4: operational outputs (read-only) ---
EXPORT prioritised_opportunities.csv   # ranked by band then score
EXPORT analyst_review_queue.csv        # requires_review = 1, ordered by priority
EXPORT duplicate_review_queue.csv      # suspected_duplicates joined to both companies
EXPORT import_exceptions.csv           # rejected rows + reasons
PRINT  recommendation summary          # band/confidence counts, top 10, queue sizes
```

---

## 6b. Stage 4 — operational outputs

Stage 4 is read-only. It turns the scored data into analyst-ready artefacts. No new tables — outputs are derived views written as CSVs (open directly in Excel / Sheets) plus a terminal summary.

```mermaid
flowchart LR
    subgraph SRC["Source tables"]
        VO["vc_opportunities"]
        PRI["vc_opportunity_priority"]
        DUP["suspected_duplicates"]
        EXC["vc_opportunity_exceptions"]
    end

    subgraph OUT["outputs/ (CSV)"]
        O1["prioritised_opportunities.csv"]
        O2["analyst_review_queue.csv"]
        O3["duplicate_review_queue.csv"]
        O4["import_exceptions.csv"]
    end

    SUMMARY["Terminal recommendation summary\n(band + confidence counts, top 10, queue sizes)"]

    VO --> O1
    PRI --> O1
    VO --> O2
    DUP --> O3
    VO --> O3
    EXC --> O4
    SRC --> SUMMARY
```

| Output | Who uses it | Answers |
|--------|-------------|---------|
| `prioritised_opportunities.csv` | Investment team | "What should we look at first?" |
| `analyst_review_queue.csv` | Data analyst | "What needs human attention before it can be trusted?" |
| `duplicate_review_queue.csv` | Data analyst | "Which records might be the same company?" |
| `import_exceptions.csv` | Data analyst | "What was rejected at import and why?" |

---

## 7. Duplicate detection tiers

Duplicates are flagged for review — never auto-merged or deleted.

```mermaid
flowchart TD
    GRP["Group records by\nnormalised company name"]

    GRP --> T1{"Description\nalso matches?"}
    T1 -->|Yes| HIGH["High confidence\n→ suspected_duplicates\n→ requires_review (high)"]
    T1 -->|No| T2{"Geography + sector\nalso match?"}
    T2 -->|Yes| MED["Medium confidence\n→ suspected_duplicates\n→ requires_review (high)"]
    T2 -->|No| LOW["Low confidence\n(name only)\n→ flagged in report only\n→ not persisted"]
```

---

## 8. What is deterministic vs AI?

| Decision | Type | Rationale |
|----------|------|-----------|
| Field validation & routing | Deterministic | Auditable, explainable rules |
| Stage / geography normalisation | Deterministic lookup | Fixed mappings, no guessing |
| Stage / geography typo correction | AI fallback | Only when lookup fails; corrects into known categories |
| Missing sector inference | AI inference | No deterministic source; flagged with confidence |
| Sector / geography / stage / referral scoring | Deterministic | Predictable, auditable matrix |
| Traction / founder scoring | AI-assisted | Requires judgement on free-text; `based_on_inferred = 1` |
| Priority band assignment | Deterministic (planned) | Sum of boolean dimension scores |

**Principle:** AI is used only where rules cannot decide. Every AI-derived value carries a confidence rating and an audit row — it never silently enters the pipeline as fact.

---

## 9. Implementation status

| Component | Status |
|-----------|--------|
| CSV import → `raw_import` | Done |
| Validation & classification | Done |
| Routing → opportunities / exceptions | Done |
| Duplicate detection & `suspected_duplicates` | Done |
| Analyst review flags | Done |
| Stage 2 normalisation + audit trail | Done |
| Stage 3 dimension scoring (6 dimensions) | Done (terminal by default; `--write` to persist) |
| Priority band + confidence → `vc_opportunity_priority` | Done (written with `--write`) |
| Stage 4 operational outputs / review queues | Done (CSV exports + summary) |
