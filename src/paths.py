"""Loads machine-local paths and pipeline settings from config.yaml.

Every other script in src/ imports from here instead of hardcoding
absolute paths, so the pipeline can run unmodified on a different
machine by editing config.yaml alone.
"""

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = REPO_ROOT / "config.yaml"

if not _CONFIG_PATH.exists():
    raise FileNotFoundError(
        f"{_CONFIG_PATH} not found. Copy config.yaml.example to config.yaml "
        "and fill in the paths for your machine."
    )

with open(_CONFIG_PATH) as f:
    _config = yaml.safe_load(f)

DATA_ROOT = Path(_config["paths"]["data_root"])
SHAPEFILE_ROOT = Path(_config["paths"]["shapefile_root"])

RAW_DIR = REPO_ROOT / "data" / "raw"
INTERIM_DIR = REPO_ROOT / "data" / "interim"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
for _d in (RAW_DIR, INTERIM_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Keys are the vintage's own year (1990, 2000, 2010, 2020, ...) — NOT a
# claim decade. Which vintage a given claim uses is a run-time choice (see
# triangulate_claims.py's --block-group-strategy/--zcta-strategy); this is
# just the set of what's available to choose from.
BLOCK_GROUP_VINTAGES = {int(k): v for k, v in _config["block_group_vintages"].items()}
ZCTA_VINTAGES = {int(k): v for k, v in _config["zcta_vintages"].items()}

BLOCK_GROUP_DEFAULT_CUTOVER_YEAR = _config["matching_strategy_defaults"]["block_group_cutover_year"]
ZCTA_DEFAULT_COVERAGE_START_YEAR = _config["matching_strategy_defaults"]["zcta_coverage_start_year"]

PLUVIAL_SEARCH_WINDOW_DAYS = _config["pluvial_correction"]["search_window_days"]
PLUVIAL_MIN_HOURLY_PRECIP_MM = _config["pluvial_correction"]["min_hourly_precip_mm"]
AORC_COVERAGE_START = _config["pluvial_correction"]["aorc_coverage_start"]

# FEMA's causeOfDamage code for pluvial (rain-driven) claims, used as a
# proxy rather than a clean label.
# Shared here so triangulate_claims.py's --cause-of-damage filter and
# build_aorc_pixel_day_index.py's pluvial-only restriction can't drift
# out of sync with each other.
PLUVIAL_CAUSE_CODE = "4"

# Raw / interim / processed file names, referenced by multiple pipeline steps.
RAW_CLAIMS_PARQUET = RAW_DIR / "FimaNfipClaims.parquet"
FRED_INFLATION_GLOB = str(RAW_DIR / "FRED_Inflation_DPCERD3Q086SBEA_*.csv")

INFLATION_ADJUSTED_PARQUET = INTERIM_DIR / "FimaNfipClaims_InflationAdjusted.parquet"
TRIANGULATED_PARQUET = INTERIM_DIR / "triangulated_claims.parquet"
AORC_REQUEST_INDEX_PARQUET = INTERIM_DIR / "aorc_request_index.parquet"
CLAIM_PIXEL_LOOKUP_PARQUET = INTERIM_DIR / "pluvial_claim_pixel_lookup.parquet"
EXCLUDED_PLUVIAL_CLAIMS_PARQUET = INTERIM_DIR / "pluvial_claims_excluded_from_correction.parquet"
AORC_HOURLY_PARQUET = INTERIM_DIR / "aorc_hourly_precip.parquet"
PLUVIAL_CORRECTED_PARQUET = INTERIM_DIR / "claims_pluvial_corrected.parquet"

FINAL_CLAIMS_PARQUET_TEMPLATE = str(PROCESSED_DIR / "nfip_claims_refined_{year}.parquet")
