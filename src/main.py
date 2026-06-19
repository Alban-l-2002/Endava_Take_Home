"""Main entry point for the VC pipeline prototype."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.db_setup import create_database, rebuild_pipeline_schema
from src.stage1_import import DEFAULT_CSV_PATH, run_stage1
from src.stage2_normalise import run_stage2
from src.sector_inference import run_sector_inference
from src.stage3_score import run_stage3
from src.stage4_outputs import run_stage4


def run_full_pipeline(
    csv_path: Path = DEFAULT_CSV_PATH,
    *,
    rebuild: bool = False,
    write_scores: bool = False,
) -> None:
    """Run all pipeline stages in order."""
    if rebuild:
        rebuild_pipeline_schema(clear_staging=True)
    else:
        create_database()
    run_stage1(csv_path=csv_path, import_if_empty=not rebuild, force_reimport=rebuild)
    print()
    run_stage2()
    print()
    run_stage3(write=write_scores)
    print()
    run_stage4()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the VC pipeline.")
    parser.add_argument(
        "--stage",
        type=int,
        choices=[1, 2, 3, 4],
        help="Run a single stage instead of the full pipeline.",
    )
    parser.add_argument(
        "--write-scores",
        action="store_true",
        help=(
            "Stage 3: persist scores and priority bands to the database "
            "(default: terminal report only). Required for Stage 4 outputs to have data."
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Wipe all pipeline tables (including raw_import), re-import CSV, "
            "and run Stages 1–4 end to end (scores are persisted automatically)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Stage 2 only: preview normalisation without writing to the database.",
    )
    parser.add_argument(
        "--sector-inference",
        action="store_true",
        help="Run Stage 2 sector inference via Claude API (terminal only).",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help="Path to the opportunities CSV (Stage 1 import only).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    try:
        if args.rebuild:
            run_full_pipeline(
                csv_path=args.csv,
                rebuild=True,
                write_scores=True,
            )
        elif args.sector_inference:
            create_database()
            run_sector_inference()
        elif args.stage == 1:
            run_stage1(csv_path=args.csv, import_if_empty=True)
        elif args.stage == 2:
            create_database()
            run_stage2(dry_run=args.dry_run)
        elif args.stage == 3:
            create_database()
            run_stage3(write=args.write_scores)
        elif args.stage == 4:
            create_database()
            run_stage4()
        else:
            run_full_pipeline(
                csv_path=args.csv,
                rebuild=args.rebuild,
                write_scores=args.write_scores,
            )
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
