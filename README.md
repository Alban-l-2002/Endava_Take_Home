# VC Pipeline — Copilot Studio Automation Challenge

A Python prototype for automating venture capital opportunity intake, validation, normalisation, scoring, and analyst review workflows.

## Architecture

The pipeline is organised into four stages:

| Stage | Module | Purpose |
|-------|--------|---------|
| 1 | `stage1_import.py` | CSV import, validation, classification, and routing |
| 2 | `stage2_normalise.py` | Normalisation, confidence ratings, and controlled AI inference |
| 3 | `stage3_score.py` | Deterministic scoring and priority assignment |
| 4 | `stage4_outputs.py` | Operational outputs and analyst review queues |

Supporting modules:

| Module | Purpose |
|--------|---------|
| `classification_rules.py` | Configurable rules for Valid / Incomplete / Ambiguous / Invalid classification |
| `validators.py` | Validation logic that applies those rules to raw records |

**Current scope:** All four stages are implemented — import, validation, routing, duplicate detection, normalisation with AI fallback/inference, 6-dimension scoring, priority bands with confidence ratings, and analyst-ready CSV outputs. See [docs/PIPELINE_WORKFLOW.md](docs/PIPELINE_WORKFLOW.md) for the full workflow artefact.

## Workflow & decision logic

Full Mermaid diagrams (data flow, routing decision trees, normalisation flow, scoring pseudo-code, analyst review paths):

**[docs/PIPELINE_WORKFLOW.md](docs/PIPELINE_WORKFLOW.md)**

## Data flow

```
CSV → raw_import → classify & route → vc_opportunities | vc_opportunity_exceptions
     → normalise (+ audit) → score (6 dimensions) → priority (planned) → outputs (planned)
```

## Why a raw staging table?

The `raw_import` table stores values exactly as they appear in the source CSV:

- **Auditability** — every downstream decision can be traced to the original input.
- **Traceability** — `source_row_number` links each record back to a specific CSV line.
- **Reprocessing** — later stages can be re-run without re-reading the CSV.
- **Debugging** — issues in validation or normalisation can be compared against untouched source data.

## Storage

SQLite (`vc_pipeline.db`) is used as a lightweight prototype equivalent of Microsoft Dataverse. Dataverse remains the intended production storage target for Copilot Studio integration.

## Identifiers

| Field | Meaning |
|-------|---------|
| `raw_id` | Internal database primary key assigned on insert into `raw_import`. |
| `source_row_number` | Original physical line number in the CSV (header = line 1, first data row = line 2). |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Place the dataset at:

```
data/vc_opportunities_dataset.csv
```

## Run

Each stage is independently executable and testable. `main.py` orchestrates the full workflow.

**Full pipeline** (import only if staging is empty, then run all stages):

```bash
python -m src.main --write-scores
```

**Full rebuild** (wipe all tables, re-import CSV, run Stages 1–4 end to end):

```bash
python -m src.main --rebuild
```

Requires `ANTHROPIC_API_KEY` in `.env` (Stages 2–3 call Claude). Writes scores to the database and CSV exports to `outputs/`.

**Single stage via main:**

```bash
python -m src.main --stage 1
python -m src.main --stage 2
```

**Single stage directly:**

```bash
python -m src.stage1_import
python -m src.stage2_normalise
python -m src.stage3_score
python -m src.stage4_outputs
```

Stage 1 imports the CSV only when `raw_import` is empty. Subsequent runs classify existing staged data without re-importing.

## Outputs

Stage 4 writes analyst-ready CSV exports to `outputs/`:

| File | Purpose |
|------|---------|
| `prioritised_opportunities.csv` | All opportunities ranked by priority band then score |
| `analyst_review_queue.csv` | Records flagged for human review, by priority |
| `duplicate_review_queue.csv` | Suspected duplicate pairs, side by side |
| `import_exceptions.csv` | Rows rejected at import, with reasons |

To populate the database and generate outputs end to end (without wiping existing data):

```bash
python -m src.main --write-scores
```

Or step by step:

```bash
python -m src.main --stage 3 --write-scores   # writes scores + priority bands
python -m src.main --stage 4                   # generates CSV exports + summary
```

## Current limitations

- Stage 3 scores are terminal-only by default; pass `--write-scores` to persist scores and priority. Stage 4 needs persisted data to produce non-empty exports.
- AI calls (sector inference, normalisation fallback, traction/founder scoring) require `ANTHROPIC_API_KEY` in `.env`.
- Re-import is skipped if `raw_import` already contains records unless `--rebuild` is used.
